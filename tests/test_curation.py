import pytest
from sqlalchemy import select

from gatekeep.curation import (
    CURATED_JUDGE_CRITERIA,
    curate_cases,
    list_unreviewed,
    review_case,
)
from gatekeep.evals import create_suite
from gatekeep.models import ApiKey, EvalCase
from gatekeep.samples import record_request_sample


async def _seed_samples(session, prompt_name, n):
    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    for i in range(n):
        await record_request_sample(
            session,
            key_id=key.id,
            prompt_name=prompt_name,
            model="m",
            input_messages=[{"role": "user", "content": f"q{i}"}],
            output_text=f"a{i}",
        )


async def test_curate_writes_unreviewed_llm_judge_cases(session):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 3)

    cases = await curate_cases("p", session, limit=2)
    assert len(cases) == 2
    for c in cases:
        assert c.reviewed is False
        assert c.source == "curated"
        assert c.check_type == "llm_judge"
        assert c.judge_criteria == CURATED_JUDGE_CRITERIA


async def test_curate_requires_a_suite(session):
    await _seed_samples(session, "p", 1)
    with pytest.raises(ValueError):
        await curate_cases("p", session, limit=1)


async def test_review_approve_marks_reviewed_and_reject_deletes(session):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 2)
    cases = await curate_cases("p", session, limit=2)

    await review_case(cases[0].id, session, approve=True)
    await review_case(cases[1].id, session, approve=False)

    remaining = (await session.execute(select(EvalCase))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == cases[0].id
    assert remaining[0].reviewed is True

    assert await list_unreviewed("p", session) == []
