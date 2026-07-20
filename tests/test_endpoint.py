import json

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.evals import create_suite
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, EvalRun, RequestLog
from gatekeep.prompts import create_prompt, get_active_prompt_version
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


@pytest.fixture(autouse=True)
async def _clean_cache():
    """Flush exact-cache and rate-limit keys so tests in this file don't collide."""
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
        assert "temperature" not in payload
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
    monkeypatch.setitem(app_module._providers, "openai", fake)
    monkeypatch.setitem(app_module._providers, "google", fake)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_requires_auth(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


async def test_missing_auth_returns_openai_shaped_401(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["type"] == "authentication_error"


async def test_invalid_body_returns_openai_shaped_400(client, raw_key):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "gpt-4o"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"


async def test_non_streaming_completion(client, raw_key):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "temperature": 0.7,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["usage"]["total_tokens"] == 4


async def test_openai_prefixed_model_routes_to_openai_provider_response(
    client, raw_key
):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "gpt-4o"
    assert body["choices"][0]["message"]["content"] == "pong"


async def test_streaming_completion(client, raw_key):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        chunks = [line async for line in r.aiter_lines()]
    text = "".join(chunks)
    assert "chat.completion.chunk" in text
    assert '"content":"po"' in text
    assert "[DONE]" in text


async def test_non_streaming_completion_logs_request(client, raw_key, session):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    response_id = r.json()["id"]

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == response_id)
        )
    ).scalar_one()
    assert log.model == "claude-sonnet-5"
    assert log.prompt_tokens == 3
    assert log.completion_tokens == 1
    assert log.total_tokens == 4
    assert log.cached is False
    assert log.cache_key is None


async def test_streaming_completion_logs_request(client, raw_key, session):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        chunks = [line async for line in r.aiter_lines()]
    first_chunk = json.loads(chunks[0].removeprefix("data: "))
    response_id = first_chunk["id"]

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == response_id)
        )
    ).scalar_one()
    assert log.prompt_tokens == 3
    assert log.completion_tokens == 2
    assert log.total_tokens == 5


async def test_prompt_name_substitutes_active_template_as_system_message(
    client, raw_key, session
):
    await create_prompt("system-context", "You are a pirate.", session)
    captured = {}
    original_complete = app_module._providers["anthropic"].complete

    async def recording_complete(payload):
        captured["payload"] = payload
        return await original_complete(payload)

    app_module._providers["anthropic"].complete = recording_complete
    try:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "gpt-4o",
                "prompt_name": "system-context",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    finally:
        app_module._providers["anthropic"].complete = original_complete
    assert r.status_code == 200
    assert captured["payload"]["system"] == "You are a pirate."


async def test_rate_limit_exhaustion_returns_429_with_retry_after(
    client, raw_key, monkeypatch
):
    """Drain a key's token bucket through the real HTTP endpoint and confirm
    the request that exceeds it gets a real 429 with a Retry-After header
    (not just at the require_rate_limit dependency level)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_tokens_per_min", 2)
    monkeypatch.setattr(settings, "rate_limit_refill_rate", 2 / 60)

    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "rate-limit-me"}],
    }
    for _ in range(2):
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body,
        )
        assert r.status_code == 200

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r.status_code == 429
    assert "retry-after" in r.headers
    assert int(r.headers["retry-after"]) >= 1
    body = r.json()
    assert body["error"]["type"] == "rate_limit_error"


async def test_unknown_prompt_name_returns_openai_shaped_400(client, raw_key):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "prompt_name": "does-not-exist",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"


async def _seed_passing_eval_for_cheaper_model(session, prompt_name, cheap_model):
    """Create `prompt_name` with an active version, register its eval suite, and
    record a passing EvalRun for `cheap_model` so `select_model` will route to it.

    Returns nothing; the caller only needs the side effects committed to `session`.
    """
    await create_prompt(prompt_name, "You are a pirate.", session)
    version = await get_active_prompt_version(prompt_name, session)
    suite = await create_suite(prompt_name, session, pass_threshold=0.9)
    session.add(
        EvalRun(
            suite_id=suite.id,
            prompt_version_id=version.id,
            model=cheap_model,
            score=0.95,
            passed=True,
            report=[],
        )
    )
    await session.commit()


async def test_route_by_cost_with_prompt_name_substitutes_cheaper_qualifying_model(
    client, raw_key, session
):
    """route_by_cost=true + prompt_name set + a cheaper model with a passing
    EvalRun at/above the quality floor must actually substitute the model:
    the provider is called with the cheaper model and `routed_from` records
    the originally-requested model on the resulting RequestLog row."""
    cheap_model = "claude-haiku-4-5-20251001"
    await _seed_passing_eval_for_cheaper_model(session, "system-context", cheap_model)

    captured = {}
    original_complete = app_module._providers["anthropic"].complete

    async def recording_complete(payload):
        captured["payload"] = payload
        return await original_complete(payload)

    app_module._providers["anthropic"].complete = recording_complete
    try:
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "gpt-4o",
                "prompt_name": "system-context",
                "route_by_cost": True,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    finally:
        app_module._providers["anthropic"].complete = original_complete

    assert r.status_code == 200
    body = r.json()
    assert body["model"] == cheap_model
    assert captured["payload"]["model"] == cheap_model

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == body["id"])
        )
    ).scalar_one()
    assert log.model == cheap_model
    assert log.routed_from == "claude-sonnet-5"


async def test_route_by_cost_without_prompt_name_is_a_noop(client, raw_key, session):
    """route_by_cost=true without prompt_name must never substitute, even
    though a cheaper qualifying model exists elsewhere: routing requires
    both fields to be set."""
    cheap_model = "claude-haiku-4-5-20251001"
    await _seed_passing_eval_for_cheaper_model(session, "system-context", cheap_model)

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "route_by_cost": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "claude-sonnet-5"

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == body["id"])
        )
    ).scalar_one()
    assert log.model == "claude-sonnet-5"
    assert log.routed_from is None


async def test_route_by_cost_defaults_to_false_and_never_substitutes(
    client, raw_key, session
):
    """route_by_cost omitted (defaulting to False) must never substitute the
    model, even when prompt_name is set and a cheaper qualifying model
    exists - this is the 'never silently override' invariant."""
    cheap_model = "claude-haiku-4-5-20251001"
    await _seed_passing_eval_for_cheaper_model(session, "system-context", cheap_model)

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "prompt_name": "system-context",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "claude-sonnet-5"

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == body["id"])
        )
    ).scalar_one()
    assert log.model == "claude-sonnet-5"
    assert log.routed_from is None
