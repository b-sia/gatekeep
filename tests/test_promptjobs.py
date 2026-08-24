from __future__ import annotations

import pytest

from gatekeep import promptjobs
from gatekeep.middleware.ratelimit import get_redis


@pytest.fixture
def redis():
    return get_redis()


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
