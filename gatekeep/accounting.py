from __future__ import annotations

import logging

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.middleware.budget import record_spend
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import RequestLog

logger = logging.getLogger(__name__)

# Per-model USD pricing as (input_price_per_1m_tokens, output_price_per_1m_tokens).
# Models not listed here (e.g. locally-served Ollama models) cost $0.
# claude-haiku-4-5-20251001 pricing is an approximate estimate consistent with
# Haiku's usual cost tier relative to Sonnet; reconcile with current published
# Anthropic pricing before relying on it for exact billing.
# OpenAI/Google prices below are approximate published per-1M-token rates as
# of this writing; reconcile with current provider pricing before relying on
# them for exact billing. Costs use the resolved (prefix-stripped) model id
# returned by resolve_route, e.g. "gpt-4o" not "openai/gpt-4o".
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-flash-latest": (1.5, 9.0),  # gemini-3.5-flash pricing as of 2026-07-21
}


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate the USD cost of a completion from its model and token counts.

    Looks up per-million-token input/output pricing in MODEL_PRICING; models
    with no listed pricing (e.g. local Ollama models) are treated as free.
    """
    input_price, output_price = MODEL_PRICING.get(model, (0.0, 0.0))
    return (prompt_tokens / 1_000_000 * input_price) + (
        completion_tokens / 1_000_000 * output_price
    )


async def log_request(
    session: AsyncSession,
    *,
    key_id: int,
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
) -> RequestLog:
    """Persist one completed request as a `RequestLog` row and commit it.

    Cost is derived via calculate_cost, unless `cost_usd_override` is given,
    in which case that value is used directly (e.g. a semantic-cache hit
    logging the original generation's cost instead of $0). `cached`/
    `cache_key` default to a non-cache-hit request. `prompt_name`,
    `routed_from`, and `prompt_version_num` are optional request-level
    metadata, defaulting to None. `prompt_version_num` records which
    PromptVersion (active or A/B candidate) actually served the request, so
    cost/eval/quality can later be compared active-vs-candidate by version.
    `cache_key` default to a non-cache-hit request.

    Also best-effort increments the key's current-period Redis spend counter
    (`budget.record_spend`) so `require_budget` can enforce a monthly cap
    without aggregating `request_logs` on every request. A Redis outage here
    only degrades that accelerator (the next budget check falls back to a
    DB aggregate) - it never fails this call or drops the RequestLog row.

    A cache hit contributes $0 to that budget counter even though `cost_usd`
    itself (and therefore `request_logs.cost_usd`) still records the full
    notional cost of the response: `request_logs.cost_usd` intentionally
    represents value delivered (used for cost/chargeback dashboards and
    paired with `cache_cost_saved_usd` as the discount), while the budget
    cap is a "spend" control meant to track what gatekeep actually pays
    upstream - a served-from-cache response has no such spend.
    """
    cost_usd = (
        cost_usd_override
        if cost_usd_override is not None
        else calculate_cost(model, prompt_tokens, completion_tokens)
    )
    log = RequestLog(
        key_id=key_id,
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
    )
    session.add(log)
    await session.commit()
    try:
        await record_spend(
            get_redis(), key_id=key_id, cost_usd=0.0 if cached else cost_usd
        )
    except RedisError:
        # Best-effort accelerator: a missed increment here just means the
        # next budget check falls back to a DB aggregate (get_period_spend),
        # not that spend goes untracked or the request fails.
        logger.warning(
            "Failed to record spend for budget tracking (Redis unavailable).",
            extra={"key_id": key_id},
        )
    return log
