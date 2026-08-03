import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, RequestLog
from gatekeep.prompts import add_prompt_version, create_prompt, set_candidate_version
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


@pytest.fixture(autouse=True)
async def _clean_cache():
    redis = get_redis()
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)


class FakeProvider:
    async def complete(self, payload):
        return CompletionResult(
            text="pong", input_tokens=3, output_tokens=1, stop_reason="end_turn"
        )

    async def stream(self, payload):
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
async def client(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", fake)
    monkeypatch.setitem(app_module._providers, "ollama", fake)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_non_streaming_message(client, raw_key):
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "pong"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 1}


async def test_streaming_message(client, raw_key):
    async with client.stream(
        "POST",
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        body = b"".join([chunk async for chunk in r.aiter_bytes()]).decode()
    assert "event: message_start" in body
    assert "event: content_block_delta" in body
    assert '"text": "po"' in body
    assert '"text": "ng"' in body
    assert "event: message_delta" in body
    assert '"stop_reason": "end_turn"' in body
    assert "event: message_stop" in body


async def test_streaming_error_emits_anthropic_shaped_error_event(
    client, raw_key, monkeypatch
):
    class FailingProvider:
        async def stream(self, payload):
            raise RuntimeError("boom")
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setitem(app_module._providers, "anthropic", FailingProvider())
    async with client.stream(
        "POST",
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        body = b"".join([chunk async for chunk in r.aiter_bytes()]).decode()
    assert "event: error" in body
    assert '"type": "error", "error": {"type": "api_error", "message": "boom"}' in body


async def test_missing_auth_returns_anthropic_shaped_401(client):
    r = await client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


async def test_cache_hit_shared_with_chat_completions_endpoint(client, raw_key):
    # First call via /v1/chat/completions populates the exact cache.
    chat_body = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 50,
    }
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=chat_body,
    )
    assert r1.status_code == 200

    # Same model/messages/max_tokens via /v1/messages should hit that cache
    # entry rather than calling the provider again.
    r2 = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r2.status_code == 200
    assert r2.json()["content"] == [{"type": "text", "text": "pong"}]


async def test_prompt_name_prepends_template_as_system(client, raw_key, session):
    await create_prompt("greeter", "Always say hi first.", session)
    calls = []
    original_complete = app_module._providers["anthropic"].complete

    async def recording_complete(payload):
        calls.append(payload)
        return await original_complete(payload)

    app_module._providers["anthropic"].complete = recording_complete
    try:
        r = await client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 50,
                "system": "also be polite",
                "messages": [{"role": "user", "content": "ping"}],
                "prompt_name": "greeter",
            },
        )
    finally:
        app_module._providers["anthropic"].complete = original_complete
    assert r.status_code == 200
    assert calls[0]["system"] == "Always say hi first.\n\nalso be polite"

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == r.json()["id"])
        )
    ).scalar_one()
    assert log.prompt_version_num == 1


async def test_prompt_name_candidate_at_100_pct_serves_candidate_via_messages(
    client, raw_key, session
):
    """The A/B candidate split must also apply to /v1/messages, not just
    /v1/chat/completions - both endpoints share resolve_prompt_version_for_request."""
    await create_prompt("greeter", "Always say hi first.", session)
    await add_prompt_version("greeter", "Always say bye first.", session)
    await set_candidate_version("greeter", 2, 100.0, session)

    calls = []
    original_complete = app_module._providers["anthropic"].complete

    async def recording_complete(payload):
        calls.append(payload)
        return await original_complete(payload)

    app_module._providers["anthropic"].complete = recording_complete
    try:
        r = await client.post(
            "/v1/messages",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "ping"}],
                "prompt_name": "greeter",
            },
        )
    finally:
        app_module._providers["anthropic"].complete = original_complete
    assert r.status_code == 200
    assert calls[0]["system"] == "Always say bye first."

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == r.json()["id"])
        )
    ).scalar_one()
    assert log.prompt_version_num == 2


async def test_unknown_prompt_name_returns_anthropic_shaped_400(client, raw_key):
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "ping"}],
            "prompt_name": "does-not-exist",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


async def test_messages_non_streaming_records_latency(client, raw_key, session):
    """The Anthropic-native endpoint records the same columns as the OpenAI one."""
    response = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.duration_ms is not None and log.duration_ms > 0
    assert log.provider_ms is not None
    assert log.duration_ms >= log.provider_ms
    assert log.ttft_ms is None


async def test_messages_streaming_records_ttft(client, raw_key, session):
    async with client.stream(
        "POST",
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_lines():
            pass
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.ttft_ms is not None
    assert log.duration_ms is not None
    assert log.ttft_ms <= log.duration_ms
