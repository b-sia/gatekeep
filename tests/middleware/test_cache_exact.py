import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from gatekeep.api.openai_schemas import (
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gatekeep.middleware.cache_exact import (
    clear_cached_response,
    get_cached_response,
    hash_request,
    set_cached_response,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.storage.models import RequestLog


@pytest.fixture(autouse=True)
async def _clean_cache():
    """Flush any leftover exact-cache keys so each test starts from a clean cache.

    Scoped to this file (rather than a global conftest fixture) so tests
    that never touch caching, e.g. test_config.py's fake-REDIS_URL tests,
    don't pay for an eager Redis connection.
    """
    redis = get_redis()
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)


def _payload(**overrides):
    """Build a minimal provider-neutral payload for hashing tests, with overrides applied."""
    payload = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    payload.update(overrides)
    return payload


def _response(id="chatcmpl-1"):
    """Build a minimal ChatCompletionResponse for cache round-trip tests."""
    return ChatCompletionResponse(
        id=id,
        created=1234,
        model="claude-sonnet-5",
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content="pong"),
                finish_reason="stop",
            )
        ],
        usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )


# -- hash_request -------------------------------------------------------


def test_hash_request_is_deterministic():
    assert hash_request(_payload()) == hash_request(_payload())


def test_hash_request_differs_by_model():
    assert hash_request(_payload(model="a")) != hash_request(_payload(model="b"))


def test_hash_request_differs_by_messages():
    p1 = _payload(messages=[{"role": "user", "content": "hi"}])
    p2 = _payload(messages=[{"role": "user", "content": "bye"}])
    assert hash_request(p1) != hash_request(p2)


def test_hash_request_differs_by_stop_sequences():
    p1 = _payload()
    p2 = _payload(stop_sequences=["STOP"])
    assert hash_request(p1) != hash_request(p2)


def test_hash_request_differs_by_max_tokens():
    p1 = _payload(max_tokens=100)
    p2 = _payload(max_tokens=200)
    assert hash_request(p1) != hash_request(p2)


def test_hash_request_same_max_tokens_matches():
    p1 = _payload(max_tokens=100)
    p2 = _payload(max_tokens=100)
    assert hash_request(p1) == hash_request(p2)


def test_hash_request_includes_system_when_present():
    p1 = _payload()
    p2 = _payload(system="be nice")
    assert hash_request(p1) != hash_request(p2)


# -- get/set/clear against real Redis ------------------------------------


async def test_get_cached_response_miss_returns_none():
    redis = get_redis()
    assert await get_cached_response(redis, 1, "nonexistent") is None


async def test_set_then_get_round_trips():
    redis = get_redis()
    h = hash_request(_payload())
    response = _response()
    await set_cached_response(redis, 1, h, response, ttl_seconds=60)
    cached = await get_cached_response(redis, 1, h)
    assert cached == response


async def test_exact_cache_is_account_scoped():
    """One account's exact-cache entry is never visible to another."""
    redis = get_redis()
    h = hash_request(_payload())
    response = _response()
    await set_cached_response(redis, 1, h, response, ttl_seconds=60)
    assert await get_cached_response(redis, 1, h) is not None
    assert await get_cached_response(redis, 2, h) is None


async def test_set_cached_response_sets_ttl():
    redis = get_redis()
    h = hash_request(_payload())
    await set_cached_response(redis, 1, h, _response(), ttl_seconds=60)
    ttl = await redis.ttl(f"cache:exact:1:{h}")
    assert 0 < ttl <= 60


async def test_clear_cached_response_removes_key():
    redis = get_redis()
    h = hash_request(_payload())
    await set_cached_response(redis, 1, h, _response(), ttl_seconds=60)
    await clear_cached_response(redis, 1, h)
    assert await get_cached_response(redis, 1, h) is None


# -- wired into /v1/chat/completions --------------------------------------


async def test_second_identical_request_is_served_from_cache(client, raw_key, counting_provider):
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "ping"}]}
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert counting_provider.calls == 1
    assert r2.json()["choices"][0]["message"]["content"] == "pong"


async def test_cache_hit_logs_cached_true(client, raw_key, session):
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "ping2"}]}
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )

    log = (
        await session.execute(select(RequestLog).where(RequestLog.cached.is_(True)))
    ).scalar_one()
    assert log.cached is True
    assert log.cache_key is not None


async def test_different_requests_are_not_shared(client, raw_key, counting_provider):
    body1 = {"model": "gpt-4o", "messages": [{"role": "user", "content": "a"}]}
    body2 = {"model": "gpt-4o", "messages": [{"role": "user", "content": "b"}]}
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body1,
    )
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body2,
    )
    assert counting_provider.calls == 2


async def test_streaming_requests_bypass_cache(client, raw_key, counting_provider):
    body = {
        "model": "gpt-4o",
        "stream": True,
        "messages": [{"role": "user", "content": "stream-me"}],
    }
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    ) as r:
        [line async for line in r.aiter_lines()]
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    ) as r:
        [line async for line in r.aiter_lines()]
    assert counting_provider.calls == 2


# -- fail-open on a Redis outage -------------------------------------------


async def test_cache_lookup_failure_falls_through_to_provider(
    client, raw_key, counting_provider, monkeypatch
):
    """A Redis outage on the cache-read path must not 500 the request."""
    redis = get_redis()

    async def _broken_get(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis, "get", _broken_get)

    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "fail-open-get"}],
    }
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r.status_code == 200
    assert counting_provider.calls == 1


async def test_cache_write_failure_still_returns_provider_response(
    client, raw_key, counting_provider, monkeypatch
):
    """A Redis outage on the cache-write path must not 500 the request."""
    redis = get_redis()

    async def _broken_set(*args, **kwargs):
        raise RedisConnectionError("simulated Redis outage")

    monkeypatch.setattr(redis, "set", _broken_set)

    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "fail-open-set"}],
    }
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "pong"
    assert counting_provider.calls == 1
