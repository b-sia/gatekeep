from __future__ import annotations

import pytest
from sqlalchemy import select

from gatekeep import promptjobs
from gatekeep.db import SessionLocal
from gatekeep.evals import add_case, create_suite
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import AuditEvent, EvalRun
from gatekeep.prompts import add_prompt_version, create_prompt, get_active_prompt_version


@pytest.fixture
def redis():
    return get_redis()


class _FakeProvider:
    """A fake eval provider whose `complete` always returns a fixed response."""

    async def complete(self, payload):
        """Return a canned response object exposing `.text == "hello"`."""

        class R:
            text = "hello"

        return R()


async def test_create_job_writes_queued_record(redis):
    job_id = await promptjobs.create_job(redis, kind="eval_run", prompt_name="p1", version_num=2)
    job = await promptjobs.get_job(redis, job_id)
    assert job["id"] == job_id
    assert job["kind"] == "eval_run"
    assert job["status"] == "queued"
    assert job["prompt_name"] == "p1"
    assert job["version_num"] == 2
    assert job["progress"] == {"done": 0, "total": 0}


async def test_update_job_merges_and_preserves_fields(redis):
    job_id = await promptjobs.create_job(redis, kind="promote", prompt_name="p2", version_num=3)
    await promptjobs.update_job(redis, job_id, status="running", progress={"done": 1, "total": 4})
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "running"
    assert job["progress"] == {"done": 1, "total": 4}
    assert job["prompt_name"] == "p2"  # untouched


async def test_get_job_missing_returns_none(redis):
    assert await promptjobs.get_job(redis, "does-not-exist") is None


async def test_run_eval_job_succeeds_and_writes_run_and_audit(redis, session):
    await create_prompt("job-eval", "tmpl", session)
    suite = await create_suite("job-eval", session, pass_threshold=0.5)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="contains",
        expected="hello",
    )
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name="job-eval", version_num=None
    )
    await promptjobs.run_eval_job(
        job_id,
        prompt_name="job-eval",
        version_num=None,
        model="claude-sonnet-5",
        provider=_FakeProvider(),
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "succeeded"
    assert job["result"]["passed"] is True

    async with SessionLocal() as s:
        runs = (await s.execute(select(EvalRun))).scalars().all()
        assert len(runs) == 1
        events = (
            (await s.execute(select(AuditEvent).where(AuditEvent.action == "eval.run")))
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].result == "success"


async def test_run_eval_job_records_error_on_missing_suite(redis, session):
    await create_prompt("job-eval-nosuite", "tmpl", session)
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name="job-eval-nosuite", version_num=None
    )
    await promptjobs.run_eval_job(
        job_id,
        prompt_name="job-eval-nosuite",
        version_num=None,
        model="claude-sonnet-5",
        provider=_FakeProvider(),
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    async with SessionLocal() as s:
        events = (
            (await s.execute(select(AuditEvent).where(AuditEvent.action == "eval.run")))
            .scalars()
            .all()
        )
        assert events[0].result == "error"


async def test_run_promote_job_no_suite_succeeds(redis, session):
    await create_prompt("job-promote", "v1", session)
    await add_prompt_version("job-promote", "v2", session)
    job_id = await promptjobs.create_job(
        redis, kind="promote", prompt_name="job-promote", version_num=2
    )
    await promptjobs.run_promote_job(
        job_id,
        prompt_name="job-promote",
        version_num=2,
        provider=_FakeProvider(),
        generate_model="claude-sonnet-5",
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "succeeded"
    async with SessionLocal() as s:
        active = await get_active_prompt_version("job-promote", s)
        assert active.version_num == 2
        events = (
            (await s.execute(select(AuditEvent).where(AuditEvent.action == "prompt.promote")))
            .scalars()
            .all()
        )
        assert events[0].result == "success"


async def test_run_promote_job_blocked_by_eval_gate(redis, session):
    await create_prompt("job-promote-gate", "v1", session)
    await add_prompt_version("job-promote-gate", "v2", session)
    suite = await create_suite("job-promote-gate", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="contains",
        expected="WILL-NOT-MATCH",
    )

    class _FailProvider:
        async def complete(self, payload):
            class R:
                text = "nope"

            return R()

    job_id = await promptjobs.create_job(
        redis, kind="promote", prompt_name="job-promote-gate", version_num=2
    )
    await promptjobs.run_promote_job(
        job_id,
        prompt_name="job-promote-gate",
        version_num=2,
        provider=_FailProvider(),
        generate_model="claude-sonnet-5",
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "blocked"
    assert job["result"]["passed"] is False
    async with SessionLocal() as s:
        active = await get_active_prompt_version("job-promote-gate", s)
        assert active.version_num == 1  # NOT promoted
        events = (
            (await s.execute(select(AuditEvent).where(AuditEvent.action == "prompt.promote")))
            .scalars()
            .all()
        )
        assert events[0].result == "blocked"
