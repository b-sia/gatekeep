import pytest
from sqlalchemy import select

from gatekeep.evals import (
    EvalGateFailure,
    add_case,
    create_suite,
    get_suite_for_prompt,
    make_eval_gate,
    run_eval_suite,
    run_suite_for_prompt,
)
from gatekeep.models import Prompt, PromptVersion
from gatekeep.prompts import add_prompt_version, create_prompt
from gatekeep.providers.base import CompletionResult


class FakeProvider:
    """Provider stub returning queued texts in order, one per complete() call."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.payloads = []

    async def complete(self, payload):
        self.payloads.append(payload)
        return CompletionResult(
            text=self._texts.pop(0), input_tokens=1, output_tokens=1, stop_reason="stop"
        )


async def _prompt_version(session, template="answer helpfully"):
    prompt = Prompt(name="system-context")
    session.add(prompt)
    await session.flush()
    version = PromptVersion(
        prompt_id=prompt.id, version_num=1, template=template, active=True
    )
    session.add(version)
    await session.flush()
    return version


async def test_contains_check_scores_and_persists_report(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains",
        expected="pong",
    )
    version = await _prompt_version(session)
    provider = FakeProvider(["...pong..."])

    run = await run_eval_suite(
        suite,
        version,
        session,
        provider=provider,
        generate_model="claude-haiku-4-5-20251001",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )

    assert run.score == 1.0
    assert run.passed is True
    assert run.model == "claude-haiku-4-5-20251001"
    assert run.report[0]["passed"] is True
    # template went to system, case messages went to messages
    assert provider.payloads[0]["system"] == "answer helpfully"
    assert provider.payloads[0]["messages"] == [{"role": "user", "content": "ping"}]


async def test_llm_judge_uses_fixed_judge_model_and_parses_verdict(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "explain X"}],
        check_type="llm_judge",
        judge_criteria="on-topic and coherent",
    )
    version = await _prompt_version(session)
    # 1st call = generation, 2nd call = judge verdict
    provider = FakeProvider(["some answer", "PASS - it is on topic"])

    run = await run_eval_suite(
        suite,
        version,
        session,
        provider=provider,
        generate_model="claude-haiku-4-5-20251001",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )

    assert run.passed is True
    assert (
        provider.payloads[1]["model"] == "claude-sonnet-5"
    )  # fixed judge, not generate_model


async def test_failed_run_scores_below_threshold(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact",
        expected="pong",
    )
    version = await _prompt_version(session)
    provider = FakeProvider(["nope"])

    run = await run_eval_suite(
        suite,
        version,
        session,
        provider=provider,
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    assert run.passed is False
    assert run.score == 0.0


async def test_gate_raises_eval_gate_failure_when_run_fails(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact",
        expected="pong",
    )
    version = await _prompt_version(session)
    gate = make_eval_gate(
        provider=FakeProvider(["wrong"]),
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    with pytest.raises(EvalGateFailure):
        await gate("system-context", version, session)


async def test_gate_is_noop_when_no_suite_registered(session):
    version = await _prompt_version(session)
    gate = make_eval_gate(
        provider=FakeProvider([]),
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    # no suite for this prompt -> gate returns without calling the provider
    await gate("system-context", version, session)
    assert await get_suite_for_prompt("no-suite", session) is None


async def test_unreviewed_cases_excluded_by_default(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact",
        expected="pong",
        reviewed=False,
        source="curated",
    )
    version = await _prompt_version(session)
    provider = FakeProvider([])  # no reviewed cases -> no provider calls

    run = await run_eval_suite(
        suite,
        version,
        session,
        provider=provider,
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    # empty suite of reviewed cases scores 1.0 (vacuously passes) and calls nothing
    assert run.report == []
    assert provider.payloads == []


async def test_run_suite_for_prompt_uses_active_version_by_default(session):
    await create_prompt("system-context", "v1 template", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains",
        expected="ok",
    )
    provider = FakeProvider(["ok!"])

    run = await run_suite_for_prompt(
        "system-context",
        session,
        provider=provider,
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    assert run.passed is True


async def test_run_suite_for_prompt_can_target_a_specific_version(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains",
        expected="ok",
    )
    provider = FakeProvider(["ok"])

    run = await run_suite_for_prompt(
        "system-context",
        session,
        version_num=2,
        provider=provider,
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    # the v2 version row was evaluated
    v2 = (
        await session.execute(
            select(PromptVersion).where(PromptVersion.version_num == 2)
        )
    ).scalar_one()
    assert run.prompt_version_id == v2.id
