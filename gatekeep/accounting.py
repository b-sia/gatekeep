from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import RequestLog

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
    return log
