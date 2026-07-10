import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.api.openai_schemas import (
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.cache_exact import (
    clear_cached_response,
    get_cached_response,
    hash_request,
    set_cached_response,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, RequestLog
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


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


def test_hash_request_ignores_max_tokens():
    p1 = _payload(max_tokens=100)
    p2 = _payload(max_tokens=200)
    assert hash_request(p1) == hash_request(p2)


def test_hash_request_includes_system_when_present():
    p1 = _payload()
    p2 = _payload(system="be nice")
    assert hash_request(p1) != hash_request(p2)


# -- get/set/clear against real Redis ------------------------------------


async def test_get_cached_response_miss_returns_none():
    redis = get_redis()
    assert await get_cached_response(redis, "nonexistent") is None


async def test_set_then_get_round_trips():
    redis = get_redis()
    h = hash_request(_payload())
    response = _response()
    await set_cached_response(redis, h, response, ttl_seconds=60)
    cached = await get_cached_response(redis, h)
    assert cached == response


async def test_set_cached_response_sets_ttl():
    redis = get_redis()
    h = hash_request(_payload())
    await set_cached_response(redis, h, _response(), ttl_seconds=60)
    ttl = await redis.ttl(f"cache:exact:{h}")
    assert 0 < ttl <= 60


async def test_clear_cached_response_removes_key():
    redis = get_redis()
    h = hash_request(_payload())
    await set_cached_response(redis, h, _response(), ttl_seconds=60)
    await clear_cached_response(redis, h)
    assert await get_cached_response(redis, h) is None


# -- wired into /v1/chat/completions --------------------------------------


class CountingProvider:
    """A fake provider that counts completion calls to verify caching behavior."""

    def __init__(self):
        """Initialize the call counter to zero."""
        self.calls = 0

    async def complete(self, payload):
        """Record a call and return a fixed completion result."""
        self.calls += 1
        return CompletionResult(
            text="pong", input_tokens=3, output_tokens=1, stop_reason="end_turn"
        )

    async def stream(self, payload):
        """Record a call and yield a fixed stream of deltas."""
        self.calls += 1
        for t in ["po", "ng"]:
            yield TextDelta(text=t)
        yield StreamEnd(stop_reason="end_turn", input_tokens=3, output_tokens=2)


@pytest_asyncio.fixture
async def raw_key(session):
    raw = generate_key()
    session.add(ApiKey(name="c", key_hash=hash_key(raw)))
    await session.commit()
    return raw


@pytest_asyncio.fixture
async def counting_provider(monkeypatch):
    fake = CountingProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", fake)
    monkeypatch.setitem(app_module._providers, "ollama", fake)
    return fake


@pytest_asyncio.fixture
async def client(counting_provider):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_second_identical_request_is_served_from_cache(
    client, raw_key, counting_provider
):
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
