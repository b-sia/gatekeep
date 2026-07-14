from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.evals import add_case, get_suite_for_prompt
from gatekeep.models import EvalCase
from gatekeep.samples import recent_samples

CURATED_JUDGE_CRITERIA = "output is a coherent, on-topic response to the input"


async def curate_cases(
    prompt_name: str, session: AsyncSession, *, limit: int
) -> list[EvalCase]:
    """Mine the most recent request samples for a prompt into unreviewed eval cases.

    Each sample becomes an unreviewed `source="curated"`, `check_type="llm_judge"`
    case with a generic judge criteria as a starting point for human tightening.
    Raises ValueError if no eval suite is registered for the prompt.
    """
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        raise ValueError(f"no eval suite registered for prompt {prompt_name!r}")

    samples = await recent_samples(prompt_name, session, limit=limit)
    cases: list[EvalCase] = []
    for sample in samples:
        case = await add_case(
            suite.id,
            session,
            input_messages=sample.input_messages,
            check_type="llm_judge",
            judge_criteria=CURATED_JUDGE_CRITERIA,
            reviewed=False,
            source="curated",
        )
        cases.append(case)
    return cases


async def list_unreviewed(prompt_name: str, session: AsyncSession) -> list[EvalCase]:
    """List unreviewed curated cases for a prompt, oldest first."""
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        return []
    result = await session.execute(
        select(EvalCase)
        .where(EvalCase.suite_id == suite.id, EvalCase.reviewed.is_(False))
        .order_by(EvalCase.id)
    )
    return list(result.scalars().all())


async def review_case(case_id: int, session: AsyncSession, *, approve: bool) -> None:
    """Approve (flip reviewed=True) or reject (delete) one curated case.

    Raises ValueError if the case id does not exist.
    """
    case = await session.get(EvalCase, case_id)
    if case is None:
        raise ValueError(f"no eval case with id {case_id}")
    if approve:
        case.reviewed = True
    else:
        await session.delete(case)
    await session.commit()
