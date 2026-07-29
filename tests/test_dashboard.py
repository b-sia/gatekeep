from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select, update

from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.evals import add_case, create_suite, run_eval_suite
from gatekeep.models import ApiKey, RequestLog
from gatekeep.prompts import add_prompt_version, create_prompt, promote_prompt


@pytest_asyncio.fixture
async def raw_key(session):
    """Create and return a raw (unhashed) active API key for auth in requests."""
    raw = generate_key()
    session.add(ApiKey(name="dashboard-test", key_hash=hash_key(raw)))
    await session.commit()
    return raw


@pytest_asyncio.fixture
async def client():
    """An httpx client bound to the real ASGI app, no provider monkeypatching needed."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_log(
    session,
    *,
    key_id: int,
    model: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost_usd: float = 0.01,
    cached: bool = False,
    prompt_name: str | None = None,
    created_at: datetime | None = None,
) -> RequestLog:
    """Insert one RequestLog row directly, optionally backdating created_at."""
    log = RequestLog(
        key_id=key_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost_usd,
        cached=cached,
        response_id=f"resp-{model}-{prompt_tokens}-{completion_tokens}-{cached}",
        prompt_name=prompt_name,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    if created_at is not None:
        await session.execute(
            update(RequestLog)
            .where(RequestLog.id == log.id)
            .values(created_at=created_at)
        )
        await session.commit()
    return log


# -- auth -----------------------------------------------------------------


async def test_usage_summary_requires_auth(client):
    r = await client.get("/dashboard/api/usage/summary")
    assert r.status_code == 401


async def test_usage_timeseries_requires_auth(client):
    r = await client.get("/dashboard/api/usage/timeseries")
    assert r.status_code == 401


async def test_evals_requires_auth(client):
    r = await client.get("/dashboard/api/evals")
    assert r.status_code == 401


async def test_prompts_requires_auth(client):
    r = await client.get("/dashboard/api/prompts")
    assert r.status_code == 401


# -- usage summary ----------------------------------------------------------


async def test_usage_summary_totals_and_breakdowns(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    await _seed_log(
        session,
        key_id=key_row.id,
        model="claude-sonnet-5",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=1.0,
        prompt_name="system-context",
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=200,
        completion_tokens=100,
        cost_usd=2.0,
        cached=True,
        prompt_name="system-context",
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.05,
    )

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    body = r.json()

    assert body["request_count"] == 3
    assert body["total_tokens"] == 150 + 300 + 15
    assert body["prompt_tokens"] == 100 + 200 + 10
    assert body["completion_tokens"] == 50 + 100 + 5
    assert body["spend_usd"] == 1.0 + 0.05
    assert body["savings_usd"] == 2.0
    assert body["cost_usd"] == 3.05
    assert body["cache_hit_count"] == 1
    assert body["cache_hit_rate"] == 1 / 3

    by_model = {row["key"]: row for row in body["by_model"]}
    assert by_model["claude-sonnet-5"]["request_count"] == 1
    assert by_model["gpt-4o"]["request_count"] == 2
    assert by_model["gpt-4o"]["cost_usd"] == 2.05

    by_key = {row["key"]: row for row in body["by_key"]}
    assert by_key[str(key_row.id)]["request_count"] == 3
    assert by_key[str(key_row.id)]["label"] == "dashboard-test"

    by_prompt = {row["key"]: row for row in body["by_prompt"]}
    assert by_prompt["system-context"]["request_count"] == 2
    assert by_prompt["(none)"]["request_count"] == 1


async def test_usage_summary_respects_time_range(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)
    await _seed_log(
        session, key_id=key_row.id, model="gpt-4o", cost_usd=5.0, created_at=old
    )
    await _seed_log(
        session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now
    )

    start = (now - timedelta(days=1)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"start": start, "end": end},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["request_count"] == 1
    assert body["cost_usd"] == 1.0


async def test_usage_summary_filters_by_model(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()
    await _seed_log(session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0)
    await _seed_log(session, key_id=key_row.id, model="claude-sonnet-5", cost_usd=2.0)

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"model": "gpt-4o"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["request_count"] == 1
    assert body["cost_usd"] == 1.0


# -- usage timeseries ---------------------------------------------------


async def test_usage_timeseries_buckets_by_day(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    await _seed_log(
        session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        cost_usd=2.0,
        cached=True,
        created_at=now,
    )
    await _seed_log(
        session, key_id=key_row.id, model="gpt-4o", cost_usd=3.0, created_at=yesterday
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(days=2)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "interval": "day",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["interval"] == "day"
    assert len(body["buckets"]) == 2
    total_requests = sum(b["request_count"] for b in body["buckets"])
    total_cache_hits = sum(b["cache_hit_count"] for b in body["buckets"])
    total_cost = sum(b["cost_usd"] for b in body["buckets"])
    assert total_requests == 3
    assert total_cache_hits == 1
    assert total_cost == 6.0


async def test_usage_timeseries_includes_token_and_spend_fields(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=1.0,
        created_at=now,
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=200,
        completion_tokens=100,
        cost_usd=2.0,
        cached=True,
        created_at=now,
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "interval": "day",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["prompt_tokens"] == 300
    assert bucket["completion_tokens"] == 150
    assert bucket["cached_tokens"] == 300
    assert bucket["spend_usd"] == 1.0
    assert bucket["savings_usd"] == 2.0


async def test_usage_timeseries_accepts_minute_interval(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    await _seed_log(
        session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(minutes=5)).isoformat(),
            "end": (now + timedelta(minutes=5)).isoformat(),
            "interval": "minute",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["interval"] == "minute"
    assert sum(b["request_count"] for b in body["buckets"]) == 1


async def test_usage_timeseries_rejects_invalid_interval(client, raw_key):
    r = await client.get(
        "/dashboard/api/usage/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"interval": "fortnight"},
    )
    assert r.status_code == 400


# -- usage timeseries by model ---------------------------------------------


async def test_usage_timeseries_by_model_requires_auth(client):
    r = await client.get("/dashboard/api/usage/timeseries/by-model")
    assert r.status_code == 401


async def test_usage_timeseries_by_model_groups_by_bucket_and_model(
    client, raw_key, session
):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=1.0,
        created_at=now,
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="claude-sonnet-5",
        prompt_tokens=200,
        completion_tokens=100,
        cost_usd=2.0,
        created_at=now,
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.1,
        created_at=now,
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries/by-model",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "interval": "day",
        },
    )
    assert r.status_code == 200
    body = r.json()
    rows = {row["model"]: row for row in body["rows"]}
    assert rows["gpt-4o"]["request_count"] == 2
    assert rows["gpt-4o"]["total_tokens"] == 165
    assert rows["gpt-4o"]["cost_usd"] == 1.1
    assert rows["claude-sonnet-5"]["request_count"] == 1
    assert rows["claude-sonnet-5"]["total_tokens"] == 300
    assert rows["claude-sonnet-5"]["cost_usd"] == 2.0


# -- evals ----------------------------------------------------------------


async def test_evals_history_returns_runs_newest_first_and_filters_by_prompt(
    client, raw_key, session
):
    await create_prompt("dash-prompt-a", "template a", session)
    suite_a = await create_suite("dash-prompt-a", session, pass_threshold=0.5)
    await add_case(
        suite_a.id,
        session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="exact",
        expected="hi",
    )

    await create_prompt("dash-prompt-b", "template b", session)
    suite_b = await create_suite("dash-prompt-b", session, pass_threshold=0.5)
    await add_case(
        suite_b.id,
        session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="exact",
        expected="hi",
    )

    class _FakeProvider:
        async def complete(self, payload):
            class R:
                text = "hi"

            return R()

    from gatekeep.prompts import get_active_prompt_version

    version_a = await get_active_prompt_version("dash-prompt-a", session)
    version_b = await get_active_prompt_version("dash-prompt-b", session)

    run_a = await run_eval_suite(
        suite_a,
        version_a,
        session,
        provider=_FakeProvider(),
        generate_model="claude-sonnet-5",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    run_b = await run_eval_suite(
        suite_b,
        version_b,
        session,
        provider=_FakeProvider(),
        generate_model="claude-sonnet-5",
        judge_model="claude-sonnet-5",
        max_tokens=64,
    )

    r = await client.get(
        "/dashboard/api/evals",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    body = r.json()
    ids = [row["id"] for row in body["runs"]]
    assert run_b.id in ids and run_a.id in ids
    assert ids.index(run_b.id) < ids.index(run_a.id)  # newest first

    r2 = await client.get(
        "/dashboard/api/evals",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"prompt_name": "dash-prompt-a"},
    )
    body2 = r2.json()
    assert all(row["prompt_name"] == "dash-prompt-a" for row in body2["runs"])
    assert run_a.id in [row["id"] for row in body2["runs"]]
    assert run_b.id not in [row["id"] for row in body2["runs"]]
    assert body2["runs"][0]["passed"] is True
    assert body2["runs"][0]["version_num"] == version_a.version_num


# -- prompts ----------------------------------------------------------------


async def test_prompts_list_returns_active_version_num(client, raw_key, session):
    await create_prompt("dash-list-prompt", "v1 text", session)
    await add_prompt_version("dash-list-prompt", "v2 text", session)
    await promote_prompt("dash-list-prompt", 2, session)

    r = await client.get(
        "/dashboard/api/prompts",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    body = r.json()
    row = next(p for p in body["prompts"] if p["name"] == "dash-list-prompt")
    assert row["active_version_num"] == 2


async def test_prompt_versions_timeline_ordered_with_active_flag(
    client, raw_key, session
):
    await create_prompt("dash-timeline-prompt", "v1 text", session, created_by="alice")
    await add_prompt_version(
        "dash-timeline-prompt", "v2 text", session, created_by="bob", notes="tweak"
    )
    await promote_prompt("dash-timeline-prompt", 2, session)

    r = await client.get(
        "/dashboard/api/prompts/dash-timeline-prompt/versions",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    body = r.json()
    versions = body["versions"]
    assert [v["version_num"] for v in versions] == [1, 2]
    assert versions[0]["active"] is False
    assert versions[1]["active"] is True
    assert versions[1]["created_by"] == "bob"
    assert versions[1]["notes"] == "tweak"


async def test_prompt_versions_timeline_404_for_unknown_prompt(client, raw_key):
    r = await client.get(
        "/dashboard/api/prompts/does-not-exist/versions",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 404
