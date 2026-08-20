from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.evals import get_suite_for_prompt
from gatekeep.models import EvalRun
from gatekeep.pricing import ModelPrice, get_pricing_table


def _combined_cost(price: ModelPrice) -> float:
    """Return a single comparable cost figure (input + output per-1M price)."""
    return price.input_per_1m + price.output_per_1m


async def select_model(
    provider: str,
    requested_model: str,
    prompt_name: str,
    quality_floor: float,
    session: AsyncSession,
) -> str:
    """Pick the cheapest same-provider model that clears `quality_floor` for this prompt's suite.

    Considers only models on `provider` (the provider `resolve_route` already
    resolved for `requested_model` - a substitute must stay on the same
    upstream SDK client the caller already selected) that are strictly
    cheaper than `requested_model` and have a most-recent EvalRun with
    `passed` True and `score >= quality_floor` for the prompt's suite.
    Returns `requested_model` unchanged when no suite exists, `requested_model`
    is unpriced, or no cheaper qualifying model is found. Never returns a more
    expensive model.
    """
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        return requested_model

    table = get_pricing_table()
    requested_price = table.lookup(provider, requested_model)
    if requested_price is None:
        return requested_model
    requested_cost = _combined_cost(requested_price)

    provider_prices = table.models_for_provider(provider)
    candidates = [
        model for model, price in provider_prices.items() if _combined_cost(price) < requested_cost
    ]
    if not candidates:
        return requested_model

    best_model = requested_model
    best_cost = requested_cost
    for model in candidates:
        latest = (
            await session.execute(
                select(EvalRun)
                .where(EvalRun.suite_id == suite.id, EvalRun.model == model)
                .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None or not latest.passed or latest.score < quality_floor:
            continue
        candidate_cost = _combined_cost(provider_prices[model])
        if candidate_cost < best_cost:
            best_model = model
            best_cost = candidate_cost
    return best_model
