from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

from gatekeep.accounts.accounting import log_request
from gatekeep.middleware.budget import (
    check_budget,
    get_period_spend,
    get_period_spend_batch,
    reconcile_period_spend,
    record_spend,
    require_budget,
    run_budget_reconciliation_loop,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.observability.metrics import budget_alerts_total
from gatekeep.storage.models import Account, ApiKey
from tests.helpers import create_account, create_key


@pytest.fixture(autouse=True)
async def _clean_budget_keys():
    """Flush any leftover budget:* keys so each test starts from a clean slate."""
    redis = get_redis()
    async for key in redis.scan_iter("budget:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("budget:*"):
        await redis.delete(key)


async def _make_account_and_key(
    session, monthly_budget_usd: float | None = None
) -> tuple[Account, ApiKey]:
    """Create an account (with the given shared budget) plus one key on it.

    Budget is pooled at the account, so the cap lives on the
    account; the key is the credential used to exercise `require_budget`.
    """
    account = await create_account(session, monthly_budget_usd=monthly_budget_usd)
    key = await create_key(session, account, key_hash=f"h{account.id}")
    await session.commit()
    return account, key


async def test_record_spend_accumulates():
    redis = get_redis()
    total = await record_spend(redis, account_id=101, cost_usd=0.5)
    assert total == pytest.approx(0.5)
    total = await record_spend(redis, account_id=101, cost_usd=0.25)
    assert total == pytest.approx(0.75)


async def test_get_period_spend_prefers_redis_when_present():
    redis = get_redis()
    await record_spend(redis, account_id=102, cost_usd=1.23)
    spent = await get_period_spend(None, redis, account_id=102)
    assert spent == pytest.approx(1.23)


async def test_get_period_spend_falls_back_to_db_when_redis_cache_miss(session):
    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()
    # No record_spend call was made, so redis has no cached total for this
    # account; get_period_spend must aggregate straight from request_logs.
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(2.0)


async def test_get_period_spend_ignores_other_periods_and_accounts(session):
    """The DB-fallback aggregate sums only the target account's spend."""
    account, key = await _make_account_and_key(session)
    other_account, other_key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    await log_request(
        session,
        key_id=other_key.id,
        account_id=other_account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r2",
    )
    redis = get_redis()
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(2.0)


async def test_budget_pools_across_keys_in_one_account(session):
    """Spend under any key counts against the account's shared pool."""
    account = await create_account(session, monthly_budget_usd=1.0)
    await create_key(session, account, name="k1", key_hash="bk1")
    await create_key(session, account, name="k2", key_hash="bk2")
    await session.commit()

    redis = get_redis()
    await record_spend(redis, account_id=account.id, cost_usd=0.6)
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(0.6)
    allowed, _ = await check_budget(session, redis, account)
    assert allowed is True  # 0.6 < 1.0

    await record_spend(redis, account_id=account.id, cost_usd=0.6)
    allowed, spent = await check_budget(session, redis, account)
    assert allowed is False  # 1.2 >= 1.0, regardless of which key spent it


async def test_log_request_does_not_record_spend_for_cached_hits(session):
    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r-cached",
        cached=True,
    )
    redis = get_redis()
    # log_request treats a cache hit as $0 spend, so record_spend should
    # never have created a Redis counter for this account; get_period_spend
    # falls back to the DB, which should also report $0 (cached rows are
    # excluded from the aggregate).
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(0.0)


async def test_get_period_spend_excludes_cached_requests_on_db_fallback(session):
    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
        cached=True,
    )
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=500_000,
        completion_tokens=0,
        response_id="r2",
    )
    redis = get_redis()
    # Redis has no cached total for this account (see test above), so this
    # exercises the DB fallback: only the non-cached row's cost should count.
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(1.0)


async def test_stale_redis_counter_is_trusted_forever_without_reconciliation(session):
    """Reproduces issue #27: a present-but-wrong Redis counter never
    self-heals, because get_period_spend's DB fallback only triggers on a
    cache miss, not on staleness. Simulates a dropped record_spend
    increment (e.g. a transient Redis blip during log_request) by directly
    overwriting the Redis counter to $0 after the DB row is committed, then
    shows get_period_spend keeps trusting that wrong value indefinitely.
    """
    from gatekeep.middleware import budget as budget_module

    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()
    # Simulate the record_spend increment for this request being lost: the
    # counter reflects $0 spent instead of the true $2.00 in request_logs.
    redis_key = budget_module._spend_redis_key(account.id, budget_module._current_period())
    await redis.set(redis_key, 0.0)

    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(0.0)  # wrong - the DB truth is $2.00


async def test_reconcile_period_spend_overwrites_stale_redis_counter(session):
    """reconcile_period_spend must fix the drift the test above exposes:
    it recomputes every account's spend from request_logs and overwrites
    the Redis counter unconditionally, healing a stale-but-present key.
    """
    from gatekeep.middleware import budget as budget_module

    account, key = await _make_account_and_key(session)
    other_account, _other_key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()
    redis_key = budget_module._spend_redis_key(account.id, budget_module._current_period())
    await redis.set(redis_key, 0.0)  # dropped increment

    totals = await reconcile_period_spend(session, redis)
    assert totals[account.id] == pytest.approx(2.0)
    assert totals[other_account.id] == pytest.approx(0.0)

    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(2.0)


async def test_get_period_spend_falls_back_to_db_on_redis_error(session, monkeypatch):
    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=500_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()

    async def _broken_get(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis, "get", _broken_get)
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(1.0)


async def test_get_period_spend_batch_empty_returns_empty_dict(session):
    redis = get_redis()
    assert await get_period_spend_batch(session, redis, account_ids=[]) == {}


async def test_get_period_spend_batch_prefers_redis_when_present():
    redis = get_redis()
    await record_spend(redis, account_id=201, cost_usd=1.5)
    await record_spend(redis, account_id=202, cost_usd=2.5)
    spends = await get_period_spend_batch(None, redis, account_ids=[201, 202])
    assert spends == {201: pytest.approx(1.5), 202: pytest.approx(2.5)}


async def test_get_period_spend_batch_falls_back_to_db_for_misses_only(session, monkeypatch):
    """A mix of Redis-hit and Redis-miss accounts each get the right value,
    with the DB fallback only queried for the accounts that actually missed."""
    from gatekeep.middleware import budget as budget_module

    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()
    # log_request's own record_spend call already seeded Redis for this
    # account; delete it to force a genuine cache miss, so this test
    # actually exercises the DB-fallback path rather than the fast path.
    await redis.delete(budget_module._spend_redis_key(account.id, budget_module._current_period()))
    await record_spend(redis, account_id=203, cost_usd=0.75)

    calls: list[list[int]] = []
    original = budget_module._aggregate_spend_from_db_batch

    async def _tracking(session, account_ids, period_start):
        calls.append(list(account_ids))
        return await original(session, account_ids, period_start)

    monkeypatch.setattr(budget_module, "_aggregate_spend_from_db_batch", _tracking)

    spends = await get_period_spend_batch(session, redis, account_ids=[203, account.id])
    assert spends == {203: pytest.approx(0.75), account.id: pytest.approx(2.0)}
    assert calls == [[account.id]]

    # The DB fallback should have seeded Redis so a second call hits the fast path.
    calls.clear()
    spends_again = await get_period_spend_batch(session, redis, account_ids=[203, account.id])
    assert spends_again == {203: pytest.approx(0.75), account.id: pytest.approx(2.0)}
    assert calls == []


async def test_get_period_spend_batch_defaults_no_activity_accounts_to_zero(session):
    account, _key = await _make_account_and_key(session)
    redis = get_redis()
    spends = await get_period_spend_batch(session, redis, account_ids=[account.id])
    assert spends == {account.id: pytest.approx(0.0)}


async def test_get_period_spend_batch_falls_back_to_db_on_redis_error(session, monkeypatch):
    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=500_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()

    async def _broken_mget(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis, "mget", _broken_mget)
    spends = await get_period_spend_batch(session, redis, account_ids=[account.id])
    assert spends == {account.id: pytest.approx(1.0)}


async def test_check_budget_allows_when_unlimited(session):
    account, key = await _make_account_and_key(session, monthly_budget_usd=None)
    redis = get_redis()
    allowed, spent = await check_budget(session, redis, account)
    assert allowed is True


async def test_check_budget_allows_when_under_cap(session):
    account, key = await _make_account_and_key(session, monthly_budget_usd=10.0)
    redis = get_redis()
    await record_spend(redis, account_id=account.id, cost_usd=5.0)
    allowed, spent = await check_budget(session, redis, account)
    assert allowed is True
    assert spent == pytest.approx(5.0)


async def test_check_budget_rejects_once_spend_reaches_cap(session):
    account, key = await _make_account_and_key(session, monthly_budget_usd=10.0)
    redis = get_redis()
    await record_spend(redis, account_id=account.id, cost_usd=10.0)
    allowed, spent = await check_budget(session, redis, account)
    assert allowed is False
    assert spent == pytest.approx(10.0)


async def test_check_budget_fires_alert_only_once_per_threshold_per_period(session):
    account, key = await _make_account_and_key(session, monthly_budget_usd=10.0)
    redis = get_redis()
    await record_spend(redis, account_id=account.id, cost_usd=8.5)

    before = budget_alerts_total.labels(threshold="warning")._value.get()
    await check_budget(session, redis, account, alert_threshold=0.8)
    await check_budget(session, redis, account, alert_threshold=0.8)
    after = budget_alerts_total.labels(threshold="warning")._value.get()
    assert after - before == 1


async def test_require_budget_allows_under_cap(session):
    account, key = await _make_account_and_key(session, monthly_budget_usd=10.0)
    result = await require_budget(key=key, session=session)
    assert result.id == key.id


async def test_require_budget_rejects_with_429_once_cap_reached(session):
    account, key = await _make_account_and_key(session, monthly_budget_usd=1.0)
    redis = get_redis()
    await record_spend(redis, account_id=account.id, cost_usd=1.0)
    with pytest.raises(HTTPException) as ei:
        await require_budget(key=key, session=session)
    assert ei.value.status_code == 429
    assert ei.value.detail["error"]["type"] == "budget_exceeded_error"


async def test_run_budget_reconciliation_loop_runs_immediately(monkeypatch):
    """The loop must reconcile on start rather than waiting a full interval
    first, so drift accumulated before a deploy is healed right away."""
    from gatekeep.middleware import budget as budget_module
    from gatekeep.storage.db import SessionLocal

    calls = []

    async def _fake_reconcile(session_arg, redis_arg, *, now=None):
        calls.append(1)
        return {}

    monkeypatch.setattr(budget_module, "reconcile_period_spend", _fake_reconcile)

    redis = get_redis()
    task = asyncio.create_task(
        run_budget_reconciliation_loop(SessionLocal, redis, interval_seconds=3600)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [1]


async def test_run_budget_reconciliation_loop_survives_a_failed_cycle(monkeypatch):
    """A single cycle's exception (e.g. a DB hiccup) must not kill the
    loop - the next cycle should still run."""
    from gatekeep.middleware import budget as budget_module
    from gatekeep.storage.db import SessionLocal

    calls = []

    async def _fake_reconcile(session_arg, redis_arg, *, now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated DB hiccup")
        return {}

    monkeypatch.setattr(budget_module, "reconcile_period_spend", _fake_reconcile)

    redis = get_redis()
    task = asyncio.create_task(
        run_budget_reconciliation_loop(SessionLocal, redis, interval_seconds=0.01)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 2
