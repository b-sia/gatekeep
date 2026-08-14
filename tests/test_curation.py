import httpx
import pytest
from anthropic import APIError
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
from tests.helpers import FakeProvider, create_account


async def _seed_samples(session, prompt_name, n):
    account = await create_account(session)
    key = ApiKey(name="k", key_hash="h", account_id=account.id)
    session.add(key)
    await session.flush()
    for i in range(n):
        await record_request_sample(
            session,
            key_id=key.id,
            account_id=account.id,
            prompt_name=prompt_name,
            model="m",
            input_messages=[{"role": "user", "content": f"q{i}"}],
            output_text=f"a{i}",
        )


async def test_curate_writes_unreviewed_llm_judge_cases_with_generated_criteria(
    session,
):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 3)
    provider = FakeProvider(["criteria for q0", "criteria for q1"])

    cases = await curate_cases("p", session, limit=2, provider=provider, generate_model="m")
    assert len(cases) == 2
    for c, expected_criteria in zip(cases, ["criteria for q0", "criteria for q1"], strict=False):
        assert c.reviewed is False
        assert c.source == "curated"
        assert c.check_type == "llm_judge"
        assert c.judge_criteria == expected_criteria


async def test_curate_falls_back_to_generic_criteria_on_generation_failure(session):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 1)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    provider = FakeProvider([APIError("upstream is down", request=request, body=None)])

    cases = await curate_cases("p", session, limit=1, provider=provider, generate_model="m")
    assert len(cases) == 1
    assert cases[0].judge_criteria == CURATED_JUDGE_CRITERIA


async def test_curate_requires_a_suite(session):
    await _seed_samples(session, "p", 1)
    provider = FakeProvider([])
    with pytest.raises(ValueError):
        await curate_cases("p", session, limit=1, provider=provider, generate_model="m")


async def test_review_approve_marks_reviewed_and_reject_deletes(session):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 2)
    provider = FakeProvider(["criteria 0", "criteria 1"])
    cases = await curate_cases("p", session, limit=2, provider=provider, generate_model="m")

    await review_case(cases[0].id, session, approve=True)
    await review_case(cases[1].id, session, approve=False)

    remaining = (await session.execute(select(EvalCase))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == cases[0].id
    assert remaining[0].reviewed is True

    assert await list_unreviewed("p", session) == []
