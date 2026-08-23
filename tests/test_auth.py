import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from gatekeep.api.errors import map_provider_error
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.middleware.auth import _enforce_pre_auth_rate_limit, extract_bearer, require_api_key
from gatekeep.models import ApiKey
from gatekeep.observability.metrics import auth_failures_total, pre_auth_rate_limit_rejections_total
from gatekeep.redis_token_bucket import get_redis
from tests.helpers import create_account


def _fake_request(host: str = "1.2.3.4") -> Request:
    return Request({"type": "http", "client": (host, 12345), "headers": []})


@pytest.fixture(autouse=True)
async def _clean_preauth_bucket():
    """Flush leftover pre-auth token-bucket keys so each test starts fresh."""
    redis = get_redis()
    async for key in redis.scan_iter("ratelimit:preauth:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("ratelimit:preauth:*"):
        await redis.delete(key)


async def test_pre_auth_rate_limit_allows_within_capacity(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "pre_auth_rate_limit_tokens_per_min", 5)
    monkeypatch.setattr(settings, "pre_auth_rate_limit_refill_rate", 5 / 60)

    await _enforce_pre_auth_rate_limit(_fake_request("9.9.9.1"))


async def test_pre_auth_rate_limit_rejects_once_exhausted(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "pre_auth_rate_limit_tokens_per_min", 1)
    monkeypatch.setattr(settings, "pre_auth_rate_limit_refill_rate", 1 / 60)

    before = pre_auth_rate_limit_rejections_total._value.get()
    request = _fake_request("9.9.9.2")
    await _enforce_pre_auth_rate_limit(request)
    with pytest.raises(HTTPException) as ei:
        await _enforce_pre_auth_rate_limit(request)
    assert ei.value.status_code == 429
    assert ei.value.detail["error"]["type"] == "rate_limit_error"
    assert "Retry-After" in ei.value.headers
    assert pre_auth_rate_limit_rejections_total._value.get() - before == 1


async def test_pre_auth_rate_limit_buckets_are_independent_per_ip(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "pre_auth_rate_limit_tokens_per_min", 1)
    monkeypatch.setattr(settings, "pre_auth_rate_limit_refill_rate", 1 / 60)

    await _enforce_pre_auth_rate_limit(_fake_request("9.9.9.3"))
    # A different IP has its own bucket, unaffected by the first IP's usage.
    await _enforce_pre_auth_rate_limit(_fake_request("9.9.9.4"))


async def test_pre_auth_rate_limit_fails_open_on_redis_outage(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    redis_client = get_redis()

    async def _broken_eval(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis_client, "eval", _broken_eval)

    # Must not raise: a Redis outage on this backstop degrades to "no extra
    # protection" rather than blocking every request, unlike the post-auth
    # per-account limiter (which fails closed - see test_ratelimit.py).
    await _enforce_pre_auth_rate_limit(_fake_request("9.9.9.5"))


async def test_require_api_key_blocks_invalid_token_flood_before_db_lookup(session, monkeypatch):
    """A flood of bad tokens from one IP is rejected by the pre-auth limiter
    without ever reaching the DB, once its bucket is exhausted."""
    settings = get_settings()
    monkeypatch.setattr(settings, "pre_auth_rate_limit_tokens_per_min", 1)
    monkeypatch.setattr(settings, "pre_auth_rate_limit_refill_rate", 1 / 60)

    request = _fake_request("9.9.9.6")

    async def _call():
        await _enforce_pre_auth_rate_limit(request)
        return await require_api_key(authorization="Bearer nope", x_api_key=None, session=session)

    with pytest.raises(HTTPException) as ei:
        await _call()
    assert ei.value.status_code == 401

    db_queries = 0
    real_execute = session.execute

    async def _counting_execute(*args, **kwargs):
        nonlocal db_queries
        db_queries += 1
        return await real_execute(*args, **kwargs)

    monkeypatch.setattr(session, "execute", _counting_execute)

    with pytest.raises(HTTPException) as ei:
        await _call()
    assert ei.value.status_code == 429
    assert db_queries == 0


async def test_require_api_key_missing_token_increments_auth_failures(session):
    before = auth_failures_total._value.get()
    with pytest.raises(HTTPException):
        await require_api_key(authorization=None, x_api_key=None, session=session)
    assert auth_failures_total._value.get() - before == 1


def test_extract_bearer_prefers_authorization():
    assert extract_bearer("Bearer abc", None) == "abc"
    assert extract_bearer(None, "xyz") == "xyz"
    assert extract_bearer(None, None) is None


async def test_require_api_key_accepts_valid(session):
    raw = generate_key()
    account = await create_account(session)
    session.add(ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id))
    await session.commit()

    key = await require_api_key(authorization=f"Bearer {raw}", x_api_key=None, session=session)
    assert key.name == "c"


async def test_require_api_key_rejects_missing(session):
    with pytest.raises(HTTPException) as ei:
        await require_api_key(authorization=None, x_api_key=None, session=session)
    assert ei.value.status_code == 401
    assert ei.value.detail["error"]["type"] == "authentication_error"


async def test_require_api_key_rejects_unknown(session):
    with pytest.raises(HTTPException) as ei:
        await require_api_key(authorization="Bearer nope", x_api_key=None, session=session)
    assert ei.value.status_code == 401
    assert ei.value.detail["error"]["type"] == "authentication_error"


async def test_require_api_key_rejects_inactive(session):
    raw = generate_key()
    account = await create_account(session)
    session.add(ApiKey(name="c", key_hash=hash_key(raw), active=False, account_id=account.id))
    await session.commit()
    with pytest.raises(HTTPException) as ei:
        await require_api_key(authorization=f"Bearer {raw}", x_api_key=None, session=session)
    assert ei.value.status_code == 401
    assert ei.value.detail["error"]["type"] == "authentication_error"


def test_map_provider_error_with_status_and_message():
    class FakeAnthropicError(Exception):
        def __init__(self, status_code, message):
            super().__init__(message)
            self.status_code = status_code
            self.message = message

    exc = FakeAnthropicError(429, "rate limited")
    response = map_provider_error(exc)
    assert response.status_code == 429
    body = json.loads(response.body)
    assert body["error"]["message"] == "rate limited"
    assert body["error"]["type"] == "upstream_error"
    assert body["error"]["code"] == "provider_error"


def test_map_provider_error_fallback_defaults():
    exc = Exception("boom")
    response = map_provider_error(exc)
    assert response.status_code == 502
    body = json.loads(response.body)
    assert body["error"]["message"] == "boom"
    assert body["error"]["type"] == "upstream_error"
    assert body["error"]["code"] == "provider_error"
