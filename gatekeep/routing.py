from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounting import MODEL_PRICING
from gatekeep.evals import get_suite_for_prompt
from gatekeep.models import EvalRun


def _model_cost(model: str) -> float:
    """Return a single comparable cost figure (input + output per-1M price) for a model."""
    input_price, output_price = MODEL_PRICING.get(model, (0.0, 0.0))
    return input_price + output_price


async def select_model(
    requested_model: str,
    prompt_name: str,
    quality_floor: float,
    session: AsyncSession,
) -> str:
    """Pick the cheapest model that clears `quality_floor` for this prompt's suite.

    Considers only models strictly cheaper than `requested_model` that have a
    most-recent EvalRun with `passed` True and `score >= quality_floor` for the
    prompt's suite. Returns `requested_model` unchanged when no suite exists or
    no cheaper qualifying model is found. Never returns a more expensive model.
    """
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        return requested_model

    requested_cost = _model_cost(requested_model)
    candidates = [
        model for model in MODEL_PRICING if _model_cost(model) < requested_cost
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
        if _model_cost(model) < best_cost:
            best_model = model
            best_cost = _model_cost(model)
    return best_model
