from __future__ import annotations

import logging

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.config import get_settings
from gatekeep.middleware.budget import record_spend
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.observability.metrics import unpriced_model_total
from gatekeep.routing.pricing import BILLED_PROVIDERS, get_pricing_table, is_unpriced
from gatekeep.storage.models import RequestLog

logger = logging.getLogger(__name__)

# Maps a `pricing_miss_policy` value to the `unpriced_model_total` outcome label
# it produces. Kept beside the policy logic so the metric vocabulary and the
# setting's Literal cannot drift apart.
_MISS_OUTCOME = {"reject": "rejected", "ceiling": "ceiling", "alert_zero": "served_zero"}

# Fixed per-1M-token USD price applied to both input and output tokens for
# every `stub/*` model when `loadtest_stub_enabled` is true. Deliberately a
# flat rate rather than a pricing-table entry - `stub` is never added to
# BILLED_PROVIDERS (there are unboundedly many stub/* variants and the
# exact-match table cannot wildcard them) - so a load-test scenario can
# compute the exact request count at which a budget cap should trip. Nominal
# value, not tied to any real provider's price; see
# docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §3.
STUB_PRICE_PER_1M = 1.0


def estimate_tokens(text: str) -> int:
    """Estimate a token count for `text` using the ~4-characters-per-token
    heuristic, matching the proxy limit `gatekeep.caching.embeddings` already uses
    for the same reason: this codebase has no real tokenizer.

    Used only where an authoritative provider-reported token count is
    unavailable - a mid-stream provider error or client disconnect never
    reaches `StreamEnd`, so the failed row's tokens/cost are approximate.

    Rounds up so any non-empty text counts as at least one token; empty
    text is zero tokens.

    Args:
        text: The text to estimate a token count for.

    Returns:
        The estimated token count, always >= 0, and >= 1 for any non-empty text.
    """
    if not text:
        return 0
    return -(-len(text) // 4)


def calculate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate the USD cost of a completion from its provider, model, and token counts.

    Looks up per-million-token input/output pricing via `pricing.get_pricing_table`
    (the vendored + operator-override pricing dataset, keyed by
    ``"<provider>/<model>"``). `provider`/`model` are exactly what
    `resolve_route` returns.

    When `provider == "stub"` and `Settings.loadtest_stub_enabled` is true,
    cost is `STUB_PRICE_PER_1M` per 1M tokens on both input and output,
    checked before any pricing-table lookup - `stub` is deliberately never a
    table entry (see routing.pricing.BILLED_PROVIDERS) and this branch is
    unreachable when the flag is false, since `stub` is then not even a
    registered provider (see gatekeep.app._build_providers).

    On a pricing miss the result depends on the provider and the configured
    `pricing_miss_policy`:

    - A non-billed provider (Ollama) is always $0 - self-hosted models are
      genuinely free.
    - A billed provider under the "ceiling" policy is charged
      `pricing_ceiling_per_1m` on both input and output, so an unpriced model
      pushes budgets down rather than escaping them.
    - A billed provider under any other policy is $0. Under "reject" that case
      is unreachable on the request path (`enforce_pricing_policy` refuses it
      before this is called); under "alert_zero" the $0 is intentional.

    This function only computes the number; it neither raises nor emits the
    miss metric/alert - that is `enforce_pricing_policy`'s job, run once per
    request so repeated `calculate_cost` calls for one request can't
    double-count.
    """
    settings = get_settings()
    if provider == "stub" and settings.loadtest_stub_enabled:
        return (prompt_tokens / 1_000_000 * STUB_PRICE_PER_1M) + (
            completion_tokens / 1_000_000 * STUB_PRICE_PER_1M
        )
    price = get_pricing_table().lookup(provider, model)
    if price is not None:
        return price.cost(prompt_tokens, completion_tokens)
    if provider in BILLED_PROVIDERS and settings.pricing_miss_policy == "ceiling":
        ceiling = settings.pricing_ceiling_per_1m
        return (prompt_tokens / 1_000_000 * ceiling) + (completion_tokens / 1_000_000 * ceiling)
    return 0.0


def enforce_pricing_policy(provider: str, model: str) -> str | None:
    """Apply `pricing_miss_policy` to one request, once, and decide whether to reject it.

    Called on the request path after the provider/model are resolved (and after
    any cost-based routing substitution), before the upstream call. For a model
    that `is_unpriced` on a billed provider it records the `unpriced_model_total`
    metric and logs, then:

    - "reject": returns a client-facing error message; the caller turns it into
      a 400 and never makes the upstream call. A model gatekeep cannot price is
      one it will not serve.
    - "ceiling" / "alert_zero": returns None (the request proceeds);
      `calculate_cost` then prices it at the ceiling or at $0 respectively.

    Returns None (proceed) for any priced model and for every Ollama model.
    Emitting the metric/alert here - rather than inside `calculate_cost`, which
    runs several times per request - is what keeps one unpriced request counted
    exactly once.
    """
    if not is_unpriced(provider, model):
        return None
    settings = get_settings()
    policy = settings.pricing_miss_policy
    unpriced_model_total.labels(provider=provider, outcome=_MISS_OUTCOME[policy]).inc()
    if policy == "reject":
        logger.warning(
            "Rejecting request for unpriced model %r on billed provider %r "
            "(pricing_miss_policy='reject').",
            model,
            provider,
        )
        return (
            f"Model {model!r} has no configured pricing and will not be served "
            "(pricing_miss_policy='reject'). Add it to the pricing table "
            "(gatekeep/data/model_prices.json or a pricing_overrides_path file), "
            "or set pricing_miss_policy to 'ceiling' or 'alert_zero'."
        )
    if policy == "ceiling":
        logger.warning(
            "Serving unpriced model %r on billed provider %r at the ceiling price "
            "$%.2f/1M (pricing_miss_policy='ceiling').",
            model,
            provider,
            settings.pricing_ceiling_per_1m,
        )
        return None
    logger.warning(
        "Serving unpriced model %r on billed provider %r at $0 "
        "(pricing_miss_policy='alert_zero'); its spend is not being tracked.",
        model,
        provider,
    )
    return None


async def log_request(
    session: AsyncSession,
    *,
    key_id: int,
    account_id: int,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    response_id: str,
    cached: bool = False,
    cache_key: str | None = None,
    cost_usd_override: float | None = None,
    prompt_name: str | None = None,
    routed_from: str | None = None,
    prompt_version_num: int | None = None,
    duration_ms: float | None = None,
    provider_ms: float | None = None,
    ttft_ms: float | None = None,
    path: str | None = None,
    outcome: str = "ok",
) -> RequestLog:
    """Persist one completed request as a `RequestLog` row and commit it.

    `account_id` is the tenant the request is attributed to, derived
    server-side from the authenticated key. It is denormalized onto the row
    (rather than joined through `key_id`) so attribution survives key rotation
    or revocation.

    `provider` is the resolved upstream (`resolve_route`'s first return
    value, e.g. "anthropic"/"openai"/"google"/"ollama") and, together with
    `model`, is what `calculate_cost` keys its pricing lookup on.

    Cost is derived via calculate_cost, unless `cost_usd_override` is given,
    in which case that value is used directly (e.g. a semantic-cache hit
    logging the original generation's cost instead of $0). `cached`/
    `cache_key` default to a non-cache-hit request. `prompt_name`,
    `routed_from`, and `prompt_version_num` are optional request-level
    metadata, defaulting to None. `prompt_version_num` records which
    PromptVersion (active or A/B candidate) actually served the request, so
    cost/eval/quality can later be compared active-vs-candidate by version.

    Also best-effort increments the account's current-period Redis spend
    counter (`budget.record_spend`) so `require_budget` can enforce a monthly
    cap without aggregating `request_logs` on every request; budget is pooled
    at the account. A Redis outage here only degrades that accelerator - it
    never fails this call or drops the RequestLog row - but a dropped
    increment does leave the counter under-counted until the next periodic
    reconciliation cycle (`budget.run_budget_reconciliation_loop`) overwrites
    it from the DB aggregate; a same-request DB fallback only happens if the
    key is missing entirely, not merely stale (see issue #27).

    A cache hit contributes $0 to that budget counter even though `cost_usd`
    itself (and therefore `request_logs.cost_usd`) still records the full
    notional cost of the response: `request_logs.cost_usd` intentionally
    represents value delivered (used for cost/chargeback dashboards and
    paired with `cache_cost_saved_usd` as the discount), while the budget
    cap is a "spend" control meant to track what gatekeep actually pays
    upstream - a served-from-cache response has no such spend.

    `duration_ms`/`provider_ms`/`ttft_ms` are optional timing in milliseconds,
    defaulting to None so any caller without timing available can still log.
    `provider_ms` is None on a cache hit (no upstream call was made) and
    `ttft_ms` is None on any non-streamed request (the concept does not
    exist there). See gatekeep/observability/latency.py.

    `path` records which branch served the request ("cache_exact",
    "cache_semantic", "provider", or "stream"), matching the Prometheus
    `path` label one-for-one. It defaults to None so a caller without one
    can still log; pre-0012 rows are NULL and latency queries exclude them.

    `outcome` is one of "ok" (default), "provider_error", or
    "client_disconnect", recording how the request ended. A mid-stream
    provider failure or client disconnect still gets a row (see #17) with
    estimated tokens/cost instead of no row at all; `outcome` is what lets
    any consumer (the dashboard latency queries, a success-rate stat)
    distinguish those estimated rows from authoritative clean ones.
    """
    cost_usd = (
        cost_usd_override
        if cost_usd_override is not None
        else calculate_cost(provider, model, prompt_tokens, completion_tokens)
    )
    log = RequestLog(
        key_id=key_id,
        account_id=account_id,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost_usd,
        cached=cached,
        cache_key=cache_key,
        response_id=response_id,
        prompt_name=prompt_name,
        routed_from=routed_from,
        prompt_version_num=prompt_version_num,
        duration_ms=duration_ms,
        provider_ms=provider_ms,
        ttft_ms=ttft_ms,
        path=path,
        outcome=outcome,
    )
    session.add(log)
    await session.commit()
    try:
        await record_spend(get_redis(), account_id=account_id, cost_usd=0.0 if cached else cost_usd)
    except RedisError:
        # Best-effort accelerator: a missed increment here doesn't fail the
        # request or drop the RequestLog row, but it does leave the Redis
        # counter under-counted until the next reconciliation cycle
        # (budget.run_budget_reconciliation_loop) - get_period_spend's DB
        # fallback only triggers on a missing key, not a stale one.
        logger.warning(
            "Failed to record spend for budget tracking (Redis unavailable).",
            extra={"account_id": account_id},
        )
    return log
