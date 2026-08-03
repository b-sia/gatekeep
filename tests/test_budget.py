from __future__ import annotations

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

from gatekeep.accounting import log_request
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.budget import (
    check_budget,
    get_period_spend,
    record_spend,
    require_budget,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey
from gatekeep.observability.metrics import budget_alerts_total


@pytest.fixture(autouse=True)
async def _clean_budget_keys():
    """Flush any leftover budget:* keys so each test starts from a clean slate."""
    redis = get_redis()
    async for key in redis.scan_iter("budget:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("budget:*"):
        await redis.delete(key)


async def _make_key(session, monthly_budget_usd: float | None = None) -> ApiKey:
    raw = generate_key()
    key = ApiKey(
        name="c", key_hash=hash_key(raw), monthly_budget_usd=monthly_budget_usd
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


async def test_record_spend_accumulates():
    redis = get_redis()
    total = await record_spend(redis, key_id=101, cost_usd=0.5)
    assert total == pytest.approx(0.5)
    total = await record_spend(redis, key_id=101, cost_usd=0.25)
    assert total == pytest.approx(0.75)


async def test_get_period_spend_prefers_redis_when_present():
    redis = get_redis()
    await record_spend(redis, key_id=102, cost_usd=1.23)
    spent = await get_period_spend(None, redis, key_id=102)
    assert spent == pytest.approx(1.23)


async def test_get_period_spend_falls_back_to_db_when_redis_cache_miss(session):
    key = await _make_key(session)
    await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()
    # No record_spend call was made, so redis has no cached total for this key;
    # get_period_spend must aggregate straight from request_logs instead.
    spent = await get_period_spend(session, redis, key_id=key.id)
    assert spent == pytest.approx(2.0)


async def test_get_period_spend_ignores_other_periods_and_keys(session):
    key = await _make_key(session)
    other_key = await _make_key(session)
    await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
    )
    await log_request(
        session,
        key_id=other_key.id,
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r2",
    )
    redis = get_redis()
    spent = await get_period_spend(session, redis, key_id=key.id)
    assert spent == pytest.approx(2.0)


async def test_log_request_does_not_record_spend_for_cached_hits(session):
    key = await _make_key(session)
    await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r-cached",
        cached=True,
    )
    redis = get_redis()
    # log_request treats a cache hit as $0 spend, so record_spend should
    # never have created a Redis counter for this key; get_period_spend
    # falls back to the DB, which should also report $0 (cached rows are
    # excluded from the aggregate).
    spent = await get_period_spend(session, redis, key_id=key.id)
    assert spent == pytest.approx(0.0)


async def test_get_period_spend_excludes_cached_requests_on_db_fallback(session):
    key = await _make_key(session)
    await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        response_id="r1",
        cached=True,
    )
    await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=500_000,
        completion_tokens=0,
        response_id="r2",
    )
    redis = get_redis()
    # Redis has no cached total for this key (see test above), so this
    # exercises the DB fallback: only the non-cached row's cost should count.
    spent = await get_period_spend(session, redis, key_id=key.id)
    assert spent == pytest.approx(1.0)


async def test_get_period_spend_falls_back_to_db_on_redis_error(session, monkeypatch):
    key = await _make_key(session)
    await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=500_000,
        completion_tokens=0,
        response_id="r1",
    )
    redis = get_redis()

    async def _broken_get(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis, "get", _broken_get)
    spent = await get_period_spend(session, redis, key_id=key.id)
    assert spent == pytest.approx(1.0)


async def test_check_budget_allows_when_unlimited(session):
    key = await _make_key(session, monthly_budget_usd=None)
    redis = get_redis()
    allowed, spent = await check_budget(session, redis, key)
    assert allowed is True


async def test_check_budget_allows_when_under_cap(session):
    key = await _make_key(session, monthly_budget_usd=10.0)
    redis = get_redis()
    await record_spend(redis, key_id=key.id, cost_usd=5.0)
    allowed, spent = await check_budget(session, redis, key)
    assert allowed is True
    assert spent == pytest.approx(5.0)


async def test_check_budget_rejects_once_spend_reaches_cap(session):
    key = await _make_key(session, monthly_budget_usd=10.0)
    redis = get_redis()
    await record_spend(redis, key_id=key.id, cost_usd=10.0)
    allowed, spent = await check_budget(session, redis, key)
    assert allowed is False
    assert spent == pytest.approx(10.0)


async def test_check_budget_fires_alert_only_once_per_threshold_per_period(session):
    key = await _make_key(session, monthly_budget_usd=10.0)
    redis = get_redis()
    await record_spend(redis, key_id=key.id, cost_usd=8.5)

    before = budget_alerts_total.labels(threshold="warning")._value.get()
    await check_budget(session, redis, key, alert_threshold=0.8)
    await check_budget(session, redis, key, alert_threshold=0.8)
    after = budget_alerts_total.labels(threshold="warning")._value.get()
    assert after - before == 1


async def test_require_budget_allows_under_cap(session):
    key = await _make_key(session, monthly_budget_usd=10.0)
    result = await require_budget(key=key, session=session)
    assert result.id == key.id


async def test_require_budget_rejects_with_429_once_cap_reached(session):
    key = await _make_key(session, monthly_budget_usd=1.0)
    redis = get_redis()
    await record_spend(redis, key_id=key.id, cost_usd=1.0)
    with pytest.raises(HTTPException) as ei:
        await require_budget(key=key, session=session)
    assert ei.value.status_code == 429
    assert ei.value.detail["error"]["type"] == "budget_exceeded_error"
