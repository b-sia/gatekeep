import pytest
from fastapi import HTTPException

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.ratelimit import (
    check_rate_limit,
    get_redis,
    require_rate_limit,
)
from gatekeep.models import ApiKey
from gatekeep.observability.metrics import rate_limit_rejections_total
from tests.helpers import create_account


@pytest.fixture(autouse=True)
async def _clean_bucket():
    """Flush any leftover token-bucket keys so each test starts from a clean bucket."""
    redis = get_redis()
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)


async def test_check_rate_limit_allows_up_to_capacity():
    redis = get_redis()
    for _ in range(3):
        allowed, remaining = await check_rate_limit(
            redis, account_id=1, capacity=3, refill_rate=0.001, now=1000.0
        )
        assert allowed is True
    assert remaining == pytest.approx(0.0, abs=1e-6)


async def test_check_rate_limit_rejects_once_exhausted():
    redis = get_redis()
    for _ in range(2):
        await check_rate_limit(redis, account_id=2, capacity=2, refill_rate=0.001, now=1000.0)
    allowed, remaining = await check_rate_limit(
        redis, account_id=2, capacity=2, refill_rate=0.001, now=1000.0
    )
    assert allowed is False
    assert remaining < 1


async def test_check_rate_limit_refills_over_time():
    redis = get_redis()
    for _ in range(2):
        await check_rate_limit(redis, account_id=3, capacity=2, refill_rate=1.0, now=1000.0)
    # Bucket is now empty; a request 1s later should get exactly one new token.
    allowed, remaining = await check_rate_limit(
        redis, account_id=3, capacity=2, refill_rate=1.0, now=1001.0
    )
    assert allowed is True
    assert remaining == pytest.approx(0.0, abs=1e-6)


async def test_check_rate_limit_buckets_are_independent_per_account():
    redis = get_redis()
    await check_rate_limit(redis, account_id=4, capacity=1, refill_rate=0.001, now=1000.0)
    allowed, _ = await check_rate_limit(
        redis, account_id=5, capacity=1, refill_rate=0.001, now=1000.0
    )
    assert allowed is True


async def test_require_rate_limit_pools_across_keys_in_one_account(session, monkeypatch):
    """Two keys on one account share a single token bucket (decision 5)."""
    from gatekeep.config import get_settings
    from tests.helpers import create_key

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_tokens_per_min", 1)
    monkeypatch.setattr(settings, "rate_limit_refill_rate", 1 / 60)

    account = await create_account(session)
    k1 = await create_key(session, account, name="k1", key_hash=hash_key(generate_key()))
    k2 = await create_key(session, account, name="k2", key_hash=hash_key(generate_key()))
    await session.commit()

    # k1 consumes the account's only token; k2 (same account) is then rejected.
    await require_rate_limit(key=k1)
    with pytest.raises(HTTPException) as ei:
        await require_rate_limit(key=k2)
    assert ei.value.status_code == 429


async def test_require_rate_limit_allows_when_tokens_available(session, monkeypatch):
    from gatekeep.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_tokens_per_min", 5)
    monkeypatch.setattr(settings, "rate_limit_refill_rate", 5 / 60)

    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    result = await require_rate_limit(key=key)
    assert result.id == key.id


async def test_require_rate_limit_rejects_with_429_and_retry_after(session, monkeypatch):
    from gatekeep.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_tokens_per_min", 1)
    monkeypatch.setattr(settings, "rate_limit_refill_rate", 1 / 60)

    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    before = rate_limit_rejections_total._value.get()
    await require_rate_limit(key=key)
    with pytest.raises(HTTPException) as ei:
        await require_rate_limit(key=key)
    assert ei.value.status_code == 429
    assert ei.value.detail["error"]["type"] == "rate_limit_error"
    assert "Retry-After" in ei.value.headers
    assert int(ei.value.headers["Retry-After"]) >= 1
    assert rate_limit_rejections_total._value.get() - before == 1


async def test_require_rate_limit_fails_closed_with_503_on_redis_outage(session, monkeypatch):
    """A Redis outage during the rate-limit check must reject with 503, not 500 or pass through."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    redis_client = get_redis()

    async def _broken_eval(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis_client, "eval", _broken_eval)

    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    with pytest.raises(HTTPException) as ei:
        await require_rate_limit(key=key)
    assert ei.value.status_code == 503
    assert ei.value.detail["error"]["type"] == "service_unavailable_error"
