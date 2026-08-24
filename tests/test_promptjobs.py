from __future__ import annotations

import pytest
from sqlalchemy import select

from gatekeep import promptjobs
from gatekeep.db import SessionLocal
from gatekeep.evals import add_case, create_suite
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import AuditEvent, EvalRun
from gatekeep.prompts import create_prompt


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
