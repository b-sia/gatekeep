from __future__ import annotations

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

from gatekeep.accounting import log_request
from gatekeep.middleware.budget import (
    check_budget,
    get_period_spend,
    record_spend,
    require_budget,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import Account, ApiKey
from gatekeep.observability.metrics import budget_alerts_total
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

    Budget is pooled at the account (decision 5), so the cap lives on the
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
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    await log_request(
        session,
        key_id=other_key.id,
        account_id=other_account.id,
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r2",
    )
    redis = get_redis()
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(2.0)


async def test_budget_pools_across_keys_in_one_account(session):
    """Spend under any key counts against the account's shared pool (decision 5)."""
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


async def test_get_period_spend_falls_back_to_db_on_redis_error(session, monkeypatch):
    account, key = await _make_account_and_key(session)
    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
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
