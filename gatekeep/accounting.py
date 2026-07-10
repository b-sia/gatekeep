from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import RequestLog

# Per-model USD pricing as (input_price_per_1m_tokens, output_price_per_1m_tokens).
# Models not listed here (e.g. locally-served Ollama models) cost $0.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
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
) -> RequestLog:
    """Persist one completed request as a `RequestLog` row and commit it.

    Cost is derived via calculate_cost. `cached`/`cache_key` default to a
    non-cache-hit request, since no caching layer exists yet.
    """
    log = RequestLog(
        key_id=key_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=calculate_cost(model, prompt_tokens, completion_tokens),
        cached=cached,
        cache_key=cache_key,
        response_id=response_id,
    )
    session.add(log)
    await session.commit()
    return log
