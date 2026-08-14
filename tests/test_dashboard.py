from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select, update

from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.evals import add_case, create_suite, run_eval_suite
from gatekeep.models import ApiKey, RequestLog
from gatekeep.prompts import add_prompt_version, create_prompt, promote_prompt
from tests.helpers import create_account, create_key


@pytest_asyncio.fixture
async def raw_key(session):
    """Create and return a raw (unhashed) active API key for auth in requests."""
    raw = generate_key()
    account = await create_account(session)
    session.add(ApiKey(name="dashboard-test", key_hash=hash_key(raw), account_id=account.id))
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
    account_id: int | None = None,
    model: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost_usd: float = 0.01,
    cached: bool = False,
    prompt_name: str | None = None,
    created_at: datetime | None = None,
    path: str | None = None,
    duration_ms: float | None = None,
    provider_ms: float | None = None,
    ttft_ms: float | None = None,
    outcome: str | None = None,
) -> RequestLog:
    """Insert one RequestLog row directly, optionally backdating created_at.

    `path`/`duration_ms`/`provider_ms`/`ttft_ms` default to None, matching a
    pre-0012 row: such rows are deliberately excluded from every latency
    query, so latency tests must pass `path` explicitly. `outcome` defaults
    to None, matching a pre-0013 (or successful) row. `account_id` defaults to
    the account that owns `key_id` (looked up), so single-tenant tests need
    not pass it; multi-account scoping tests pass it explicitly.
    """
    if account_id is None:
        account_id = (
            await session.execute(select(ApiKey.account_id).where(ApiKey.id == key_id))
        ).scalar_one()
    log = RequestLog(
        key_id=key_id,
        account_id=account_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost_usd,
        cached=cached,
        response_id=f"resp-{uuid4()}",
        prompt_name=prompt_name,
        path=path,
        duration_ms=duration_ms,
        provider_ms=provider_ms,
        ttft_ms=ttft_ms,
        outcome=outcome,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    if created_at is not None:
        await session.execute(
            update(RequestLog).where(RequestLog.id == log.id).values(created_at=created_at)
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


# -- account scoping (decision 6, problem 1) --------------------------------


async def _account_with_key(session, *, name, is_operator=False):
    """Create an account and one key on it, returning (raw_key, key_id, account_id)."""
    account = await create_account(session, name=name, is_operator=is_operator)
    raw = generate_key()
    key = await create_key(session, account, name=name, key_hash=hash_key(raw))
    return raw, key.id, account.id


async def test_usage_summary_scopes_to_caller_account(client, session):
    raw_a, key_a, acct_a = await _account_with_key(session, name="A")
    raw_b, key_b, acct_b = await _account_with_key(session, name="B")
    await session.commit()

    await _seed_log(session, key_id=key_a, account_id=acct_a, model="gpt-4o", cost_usd=1.0)
    await _seed_log(session, key_id=key_b, account_id=acct_b, model="gpt-4o", cost_usd=5.0)

    r = await client.get(
        "/dashboard/api/usage/summary", headers={"Authorization": f"Bearer {raw_a}"}
    )
    assert r.status_code == 200
    body = r.json()
    # A sees only its own $1.0, not B's $5.0.
    assert body["cost_usd"] == pytest.approx(1.0)
    assert body["request_count"] == 1
    assert {row["key"] for row in body["by_key"]} == {str(key_a)}


async def test_operator_sees_fleet_wide(client, session):
    raw_a, key_a, acct_a = await _account_with_key(session, name="A")
    raw_b, key_b, acct_b = await _account_with_key(session, name="B")
    raw_op, key_op, acct_op = await _account_with_key(session, name="ops", is_operator=True)
    await session.commit()

    await _seed_log(session, key_id=key_a, account_id=acct_a, model="gpt-4o", cost_usd=1.0)
    await _seed_log(session, key_id=key_b, account_id=acct_b, model="gpt-4o", cost_usd=5.0)

    r = await client.get(
        "/dashboard/api/usage/summary", headers={"Authorization": f"Bearer {raw_op}"}
    )
    assert r.status_code == 200
    body = r.json()
    # An operator account sees both accounts' totals.
    assert body["cost_usd"] == pytest.approx(6.0)
    assert {str(key_a), str(key_b)} <= {row["key"] for row in body["by_key"]}


async def test_non_operator_cannot_read_other_account_by_key_id(client, session):
    raw_a, key_a, acct_a = await _account_with_key(session, name="A")
    raw_b, key_b, acct_b = await _account_with_key(session, name="B")
    await session.commit()

    await _seed_log(session, key_id=key_b, account_id=acct_b, model="gpt-4o", cost_usd=5.0)

    # A passes B's key_id as a filter; the account scope ANDs to empty, no leak.
    r = await client.get(
        f"/dashboard/api/usage/summary?key_id={key_b}",
        headers={"Authorization": f"Bearer {raw_a}"},
    )
    assert r.status_code == 200
    assert r.json()["request_count"] == 0


# -- usage summary ----------------------------------------------------------


async def test_usage_summary_totals_and_breakdowns(client, raw_key, session):
    key_row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
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
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    ).scalar_one()

    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    await _seed_log(session, key_id=key_row.id, model="gpt-4o", cost_usd=5.0, created_at=old)
    await _seed_log(session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now)

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
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
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


async def test_usage_summary_includes_cost_of_failed_rows(client, raw_key, session):
    """Cost/spend aggregates are unchanged by #17: a failed row's estimated
    cost still counts (the money was spent), unlike the latency percentiles
    Task 8 excludes it from."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(session, key_id=key_id, model="gpt-4o", cost_usd=0.5, outcome="ok")
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        cost_usd=0.25,
        outcome="provider_error",
    )

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["request_count"] == 2
    assert body["cost_usd"] == pytest.approx(0.75)
    assert body["spend_usd"] == pytest.approx(0.75)


async def test_usage_summary_includes_failed_count_and_success_rate(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="ok")
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome=None)
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="provider_error")
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="client_disconnect")

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["request_count"] == 4
    assert body["failed_count"] == 2
    assert body["success_rate"] == pytest.approx(0.5)


async def test_usage_summary_cache_hit_rate_ignores_failed_rows(client, raw_key, session):
    """cache_hit_rate is taken over successful requests, not the full count.
    Since #17 logs failed rows, including them in the denominator would deflate
    the rate whenever upstream failures rise, with no change in caching. Here
    one hit + one miss + two failures must read 1/2, not 1/4."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="ok", cached=True)
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="ok", cached=False)
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="provider_error")
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="client_disconnect")

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["request_count"] == 4
    assert body["cache_hit_count"] == 1
    assert body["cache_hit_rate"] == pytest.approx(0.5)


async def test_usage_summary_success_rate_is_zero_for_an_empty_window(client, raw_key):
    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()
    assert body["request_count"] == 0
    assert body["failed_count"] == 0
    assert body["success_rate"] == 0.0


# -- usage timeseries ---------------------------------------------------


async def test_usage_timeseries_buckets_by_day(client, raw_key, session):
    key_row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    ).scalar_one()

    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    await _seed_log(session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now)
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        cost_usd=2.0,
        cached=True,
        created_at=now,
    )
    await _seed_log(session, key_id=key_row.id, model="gpt-4o", cost_usd=3.0, created_at=yesterday)

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
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    ).scalar_one()

    now = datetime.now(UTC)
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
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    ).scalar_one()

    now = datetime.now(UTC)
    await _seed_log(session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now)

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


async def test_usage_timeseries_by_model_groups_by_bucket_and_model(client, raw_key, session):
    key_row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    ).scalar_one()

    now = datetime.now(UTC)
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


async def test_prompt_versions_timeline_ordered_with_active_flag(client, raw_key, session):
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


# -- dashboard SPA fallback ---------------------------------------------


async def test_dashboard_unmatched_api_path_returns_404(client):
    r = await client.get("/dashboard/api/does-not-exist")
    assert r.status_code == 404


async def test_dashboard_api_prefix_alone_returns_404(client):
    r = await client.get("/dashboard/api")
    assert r.status_code == 404


# -- latency summary --------------------------------------------------------


async def _key_id(session, raw_key: str) -> int:
    """Resolve the ApiKey id backing a raw dashboard test key."""
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
    ).scalar_one()
    return row.id


async def _seed_latency_fixture(session, key_id: int) -> None:
    """Seed the shared latency fixture used by the summary/timeseries tests.

    Five latency-eligible rows plus one pre-0012 row:

      1. provider     dur=100  prov=60   gpt-4o          prompt "p1"
      2. provider     dur=200  prov=160  gpt-4o          prompt "p1"
      3. cache_exact  dur=300  prov=NULL claude-sonnet-5 no prompt (cached)
      4. stream       dur=1000 prov=900  ttft=100  gpt-4o
      5. stream       dur=2000 prov=1800 ttft=300  gpt-4o
      6. path=NULL    dur=9e9  prov=1    - must never appear anywhere
    """
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        prompt_name="p1",
        path="provider",
        duration_ms=100.0,
        provider_ms=60.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        prompt_name="p1",
        path="provider",
        duration_ms=200.0,
        provider_ms=160.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="claude-sonnet-5",
        cached=True,
        path="cache_exact",
        duration_ms=300.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=1000.0,
        provider_ms=900.0,
        ttft_ms=100.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=2000.0,
        provider_ms=1800.0,
        ttft_ms=300.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        duration_ms=9_000_000_000.0,
        provider_ms=1.0,
    )


async def test_latency_summary_requires_auth(client):
    r = await client.get("/dashboard/api/latency/summary")
    assert r.status_code == 401


async def test_latency_summary_percentiles(client, raw_key, session):
    """p50 over an odd-sized set is an actual element, so it is exact.
    p95/p99 interpolate between elements, where binary float arithmetic
    makes `==` fragile - pytest.approx's default 1e-6 relative tolerance is
    still far tighter than any real measurement difference."""
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    body = r.json()

    # Every latency-eligible row, both streaming and not; the path=NULL row
    # is excluded.
    assert body["sample_count"] == 5

    # Non-streaming durations: {100, 200, 300}.
    assert body["e2e_ms"]["p50_ms"] == 200.0
    assert body["e2e_ms"]["p95_ms"] == pytest.approx(290.0)
    assert body["e2e_ms"]["p99_ms"] == pytest.approx(298.0)

    # Non-streaming provider times: {60, 160}. The cache hit made no upstream
    # call, so its NULL drops out of the ordered set rather than counting.
    assert body["provider_ms"]["p50_ms"] == 110.0
    assert body["provider_ms"]["p95_ms"] == pytest.approx(155.0)

    # Overhead = duration - provider on a non-cached row, duration alone on a
    # cached one: {40, 40, 300}. The cache hit's entire 300ms is gatekeep's
    # own time.
    assert body["overhead_ms"]["p50_ms"] == 40.0
    assert body["overhead_ms"]["p95_ms"] == pytest.approx(274.0)

    # Streaming time-to-last-token: {1000, 2000}.
    assert body["stream_ttlt_ms"]["p50_ms"] == 1500.0
    # Streaming TTFT: {100, 300}.
    assert body["ttft_ms"]["p50_ms"] == 200.0


async def test_latency_overhead_excludes_uncached_row_with_no_provider_ms(client, raw_key, session):
    """A non-cached row with a NULL `provider_ms` means the upstream call
    never completed (see `test_provider_error_does_not_count_whole_span_as_
    overhead` on the Prometheus side); the gateway never logs such a row
    today, but the overhead expression must not infer "no provider call" the
    way it correctly does for an actual cache hit. If it ever is logged, the
    row must drop out of the overhead percentile set, not get counted as if
    its whole duration were gateway time."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        cached=False,
        duration_ms=500.0,
        provider_ms=None,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        cached=False,
        duration_ms=100.0,
        provider_ms=80.0,
    )

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    # Both rows are latency-eligible (path set, duration_ms set) and count
    # toward e2e and the sample total...
    assert body["sample_count"] == 2
    assert body["e2e_ms"]["p50_ms"] == 300.0

    # ...but only the row with a real provider_ms feeds overhead. If the
    # uncached-NULL row leaked in as duration-minus-zero, p50 would be
    # (20 + 500) / 2 = 260 instead.
    assert body["overhead_ms"]["p50_ms"] == 20.0


async def test_latency_summary_excludes_failed_outcome_rows(client, raw_key, session):
    """A failed row's accurate duration_ms is still stored (see the design
    spec), but must not move the percentiles - only outcome='ok'/NULL rows
    are latency-eligible."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=100.0,
        provider_ms=80.0,
        outcome=None,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=9999.0,
        provider_ms=9999.0,
        outcome="provider_error",
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=9999.0,
        provider_ms=9999.0,
        outcome="client_disconnect",
    )

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["sample_count"] == 1
    assert body["stream_ttlt_ms"]["p50_ms"] == 100.0


async def test_latency_timeseries_excludes_failed_outcome_rows(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    now = datetime.now(UTC)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=100.0,
        provider_ms=80.0,
        created_at=now,
        outcome="ok",
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=9999.0,
        provider_ms=9999.0,
        created_at=now,
        outcome="provider_error",
    )

    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["sample_count"] == 1
    assert body["buckets"][0]["e2e_p50_ms"] == 100.0


async def test_latency_summary_breakdowns(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    # by_path is the one breakdown covering streaming rows too.
    by_path = {row["key"]: row for row in body["by_path"]}
    assert by_path["provider"]["sample_count"] == 2
    assert by_path["provider"]["p50_ms"] == 150.0
    assert by_path["cache_exact"]["sample_count"] == 1
    assert by_path["cache_exact"]["p50_ms"] == 300.0
    assert by_path["stream"]["sample_count"] == 2
    assert by_path["stream"]["p50_ms"] == 1500.0

    # The other three are non-streaming only, matching the top-level rule.
    by_model = {row["key"]: row for row in body["by_model"]}
    assert by_model["gpt-4o"]["sample_count"] == 2
    assert by_model["gpt-4o"]["p50_ms"] == 150.0
    assert by_model["claude-sonnet-5"]["sample_count"] == 1
    assert by_model["claude-sonnet-5"]["p50_ms"] == 300.0

    by_key = {row["key"]: row for row in body["by_key"]}
    assert by_key[str(key_id)]["sample_count"] == 3
    assert by_key[str(key_id)]["p50_ms"] == 200.0
    assert by_key[str(key_id)]["label"] == "dashboard-test"

    by_prompt = {row["key"]: row for row in body["by_prompt"]}
    assert by_prompt["p1"]["sample_count"] == 2
    assert by_prompt["p1"]["p50_ms"] == 150.0
    assert by_prompt["(none)"]["sample_count"] == 1
    assert by_prompt["(none)"]["p50_ms"] == 300.0


async def test_latency_summary_excludes_rows_with_no_path(client, raw_key, session):
    """Rows written between migrations 0011 and 0012 carry timings but no
    path, and nothing can tell a streamed one from a non-streamed one."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        duration_ms=500.0,
        provider_ms=400.0,
    )

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()
    assert body["sample_count"] == 0
    assert body["e2e_ms"] is None
    assert body["by_path"] == []


async def test_latency_summary_empty_window_returns_nulls_not_zeros(client, raw_key, session):
    """A cost-only workload must read "no data", not "0 ms"."""
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    now = datetime.now(UTC)
    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(days=40)).isoformat(),
            "end": (now - timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sample_count"] == 0
    assert body["e2e_ms"] is None
    assert body["provider_ms"] is None
    assert body["overhead_ms"] is None
    assert body["stream_ttlt_ms"] is None
    assert body["ttft_ms"] is None
    assert body["by_model"] == []


async def test_latency_summary_filters_by_model(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"model": "claude-sonnet-5"},
    )
    body = r.json()
    assert body["sample_count"] == 1
    assert body["e2e_ms"]["p50_ms"] == 300.0
    assert [row["key"] for row in body["by_path"]] == ["cache_exact"]


# -- latency timeseries -----------------------------------------------------


async def test_latency_timeseries_requires_auth(client):
    r = await client.get("/dashboard/api/latency/timeseries")
    assert r.status_code == 401


async def test_latency_timeseries_buckets_by_day(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    # Yesterday: two non-streaming rows only.
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=100.0,
        provider_ms=60.0,
        created_at=yesterday,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=200.0,
        provider_ms=160.0,
        created_at=yesterday,
    )
    # Today: one streaming row only.
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=1000.0,
        provider_ms=900.0,
        ttft_ms=250.0,
        created_at=now,
    )

    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"interval": "day"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["interval"] == "day"
    assert len(body["buckets"]) == 2

    first, second = body["buckets"]
    assert first["sample_count"] == 2
    assert first["e2e_p50_ms"] == 150.0
    assert first["provider_p50_ms"] == 110.0
    # Overhead: {40, 40}.
    assert first["overhead_p50_ms"] == 40.0
    # No streamed rows yesterday.
    assert first["ttft_p50_ms"] is None

    assert second["sample_count"] == 1
    # The only row today is streamed, so every non-streaming field is null -
    # never 0, which would read as an instantaneous response.
    assert second["e2e_p50_ms"] is None
    assert second["provider_p50_ms"] is None
    assert second["overhead_p50_ms"] is None
    assert second["ttft_p50_ms"] == 250.0


async def test_latency_timeseries_excludes_rows_with_no_path(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        duration_ms=500.0,
        provider_ms=400.0,
    )

    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    assert r.json()["buckets"] == []


async def test_latency_timeseries_empty_window_is_not_an_error(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    now = datetime.now(UTC)
    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(days=40)).isoformat(),
            "end": (now - timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 200
    assert r.json()["buckets"] == []
