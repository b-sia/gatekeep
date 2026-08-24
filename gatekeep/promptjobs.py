from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

_JOB_KEY_PREFIX = "promptjob:"
_JOB_TTL_SECONDS = 3600

# Hold strong references to in-flight background tasks so the event loop does
# not garbage-collect a task whose only reference was the local returned by
# asyncio.create_task (a documented footgun). Tasks remove themselves on
# completion via the done callback.
_tasks: set[asyncio.Task] = set()


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _key(job_id: str) -> str:
    """Return the Redis key holding the job record for `job_id`."""
    return f"{_JOB_KEY_PREFIX}{job_id}"


async def create_job(redis: Redis, *, kind: str, prompt_name: str, version_num: int | None) -> str:
    """Create a `queued` job record in Redis and return its generated id.

    Args:
        redis: The async Redis client.
        kind: `eval_run` or `promote`.
        prompt_name: The prompt the job targets.
        version_num: The target version number, or None.

    Returns:
        The new job's uuid (hex string).
    """
    job_id = uuid4().hex
    record = {
        "id": job_id,
        "kind": kind,
        "prompt_name": prompt_name,
        "version_num": version_num,
        "status": "queued",
        "progress": {"done": 0, "total": 0},
        "result": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await redis.set(_key(job_id), json.dumps(record), ex=_JOB_TTL_SECONDS)
    return job_id


async def get_job(redis: Redis, job_id: str) -> dict | None:
    """Return the job record for `job_id`, or None if it is missing/expired."""
    raw = await redis.get(_key(job_id))
    if raw is None:
        return None
    return json.loads(raw)


async def update_job(redis: Redis, job_id: str, **changes: Any) -> dict | None:
    """Merge `changes` into a job record, bump `updated_at`, and reset its TTL.

    Returns the updated record, or None if the job no longer exists (expired
    or never created). The whole-record read-modify-write is safe here
    because a single job is only ever advanced by its one owning task.
    """
    record = await get_job(redis, job_id)
    if record is None:
        return None
    record.update(changes)
    record["updated_at"] = _now()
    await redis.set(_key(job_id), json.dumps(record), ex=_JOB_TTL_SECONDS)
    return record


def spawn(coro) -> asyncio.Task:
    """Schedule `coro` as a tracked background task.

    Keeps a strong reference in `_tasks` until the task finishes, so the
    event loop cannot drop a still-running job. This is the in-process
    Approach A backbone: a process restart loses an in-flight task (the
    operator re-runs it), while completed results persist in the DB.
    """
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
