import asyncio
import time

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


class MidStreamFailureProvider:
    """Yields some deltas, then raises - mirrors test_endpoint.py's provider
    of the same name for the Anthropic-shaped streaming path."""

    async def complete(self, payload):
        raise RuntimeError("upstream exploded mid non-stream")

    async def stream(self, payload):
        yield TextDelta(text="po")
        yield TextDelta(text="ng")
        raise RuntimeError("upstream exploded mid-stream")


@pytest_asyncio.fixture
async def mid_stream_failure_client(monkeypatch):
    failing = MidStreamFailureProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", failing)
    monkeypatch.setitem(app_module._providers, "ollama", failing)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class StreamEndsWithoutMarkerProvider:
    """Yields deltas then simply stops, without ever yielding StreamEnd -
    reproduces some providers' conditional StreamEnd emission (openai.py's
    usage-chunk gate, google.py's finish_reason gate, ollama.py's done-flag
    gate), where the async generator can complete its iteration without
    ever reaching StreamEnd."""

    async def complete(self, payload):
        raise RuntimeError("not used")

    async def stream(self, payload):
        yield TextDelta(text="po")
        yield TextDelta(text="ng")


@pytest_asyncio.fixture
async def stream_ends_without_marker_client(monkeypatch):
    stubbed = StreamEndsWithoutMarkerProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", stubbed)
    monkeypatch.setitem(app_module._providers, "ollama", stubbed)
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


async def test_messages_non_streaming_records_provider_path(client, raw_key, session):
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
    assert log.path == "provider"


async def test_messages_streaming_records_stream_path(client, raw_key, session):
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
    assert log.path == "stream"
    assert log.outcome == "ok"


async def test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens(
    mid_stream_failure_client, raw_key, session
):
    async with mid_stream_failure_client.stream(
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

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "provider_error"
    assert log.completion_tokens == 1  # "po" + "ng" -> ceil(4/4)
    assert log.prompt_tokens > 0
    assert log.cost_usd > 0
    assert log.duration_ms is not None
    assert log.provider_ms is not None


async def test_stream_ending_without_streamend_marker_logs_ok_with_estimates(
    stream_ends_without_marker_client, raw_key, session
):
    """A provider whose stream() completes without ever yielding StreamEnd is
    a success, not a failure: the client received the full body. The row is
    logged outcome='ok' with estimated tokens (no authoritative count exists),
    the stream ends cleanly with synthesized content_block_stop/message_delta/
    message_stop events, and no phantom error event is surfaced."""
    async with stream_ends_without_marker_client.stream(
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
    assert "event: error" not in body
    assert "event: content_block_stop" in body
    assert "event: message_delta" in body
    assert "event: message_stop" in body

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "ok"
    assert log.completion_tokens == 1
    assert log.cost_usd > 0


async def test_client_disconnect_mid_stream_logs_failed_row(session, raw_key):
    key = ApiKey(name="messages-disconnect-test", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time.perf_counter()}
    gen = app_module._messages_sse(
        FakeProvider(),
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
        state=state,
    )
    await gen.__anext__()  # message_start
    await gen.__anext__()  # content_block_start
    await gen.__anext__()  # first content_block_delta, "po"

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "client_disconnect"
    assert log.completion_tokens == 1
    assert log.prompt_tokens > 0
    assert log.duration_ms is not None


async def test_client_disconnect_via_aclose_logs_failed_row(session, raw_key):
    """Real client disconnects are delivered via Starlette's aclose() -
    GeneratorExit thrown at the generator's suspended yield - not a
    directly-injected CancelledError. The athrow-based tests above cover
    the exception TYPE handling but not this delivery MECHANISM. aclose()
    must return normally (the generator catches and re-raises GeneratorExit,
    which is the successful-close case per the async generator protocol,
    not an error) and the row must still be written."""
    key = ApiKey(
        name="messages-aclose-disconnect-test", key_hash=hash_key(generate_key())
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time.perf_counter()}
    gen = app_module._messages_sse(
        FakeProvider(),
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
        state=state,
    )
    await gen.__anext__()  # message_start
    await gen.__anext__()  # content_block_start
    await gen.__anext__()  # first content_block_delta, "po"

    await gen.aclose()  # must return normally, not raise

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "client_disconnect"
    assert log.completion_tokens == 1


async def test_client_disconnect_before_first_token_has_null_duration(session, raw_key):
    """Cancelling right after the very first yield (message_start, before
    content_block_start or any delta) must still be caught by the try block
    and log a row - this is the same boundary condition Task 5 found a bug
    at for _sse, fixed the same way here."""
    key = ApiKey(
        name="messages-disconnect-early-test", key_hash=hash_key(generate_key())
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time.perf_counter()}
    gen = app_module._messages_sse(
        FakeProvider(),
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
        state=state,
    )
    await gen.__anext__()  # message_start only - no content_block_start yet

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "client_disconnect"
    assert log.completion_tokens == 0
    assert log.duration_ms is None


async def test_non_streaming_provider_error_logs_outcome_and_overhead(
    client, raw_key, session, monkeypatch
):
    """A non-streaming `/v1/messages` call whose provider raises must publish
    provider_ms and log a RequestLog row with outcome='provider_error', the
    same accounting fix as Task 7's chat/completions companion test."""

    class FailingProvider:
        async def complete(self, payload):
            raise RuntimeError("boom")

    monkeypatch.setitem(app_module._providers, "anthropic", FailingProvider())

    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 502

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "provider_error"
    assert log.prompt_tokens == 0
    assert log.completion_tokens == 0
    assert log.provider_ms is not None
    assert log.path == "provider"
