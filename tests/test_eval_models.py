from sqlalchemy import select

from gatekeep.models import (
    ApiKey,
    EvalCase,
    EvalRun,
    EvalSuite,
    Prompt,
    PromptVersion,
    RequestSample,
)


async def test_request_sample_round_trips_structured_messages(session):
    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    sample = RequestSample(
        key_id=key.id,
        prompt_name="system-context",
        model="claude-sonnet-5",
        input_messages=[{"role": "user", "content": "hi"}],
        output_text="hello",
    )
    session.add(sample)
    await session.commit()

    got = (await session.execute(select(RequestSample))).scalar_one()
    assert got.input_messages == [{"role": "user", "content": "hi"}]
    assert got.prompt_name == "system-context"


async def test_eval_suite_case_and_run_persist(session):
    suite = EvalSuite(name="system-context", prompt_name="system-context", pass_threshold=0.9)
    session.add(suite)
    await session.flush()

    case = EvalCase(
        suite_id=suite.id,
        input_messages=[{"role": "user", "content": "ping"}],
        expected="pong",
        check_type="contains",
        reviewed=True,
        source="manual",
    )
    session.add(case)

    prompt = Prompt(name="system-context")
    session.add(prompt)
    await session.flush()
    version = PromptVersion(prompt_id=prompt.id, version_num=1, template="t", active=True)
    session.add(version)
    await session.flush()

    run = EvalRun(
        suite_id=suite.id,
        prompt_version_id=version.id,
        model="claude-sonnet-5",
        score=1.0,
        passed=True,
        report=[{"case_id": case.id, "passed": True, "actual_output": "pong", "reason": ""}],
    )
    session.add(run)
    await session.commit()

    assert (await session.execute(select(EvalCase))).scalar_one().check_type == "contains"
    assert (await session.execute(select(EvalRun))).scalar_one().passed is True


async def test_request_log_has_prompt_name_and_routed_from(session):
    from gatekeep.models import RequestLog

    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    log = RequestLog(
        key_id=key.id,
        model="claude-haiku-4-5-20251001",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.0,
        response_id="r1",
        prompt_name="system-context",
        routed_from="claude-sonnet-5",
    )
    session.add(log)
    await session.commit()
    got = (await session.execute(select(RequestLog))).scalar_one()
    assert got.prompt_name == "system-context"
    assert got.routed_from == "claude-sonnet-5"
