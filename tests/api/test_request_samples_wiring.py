import pytest
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.prompts.prompts import create_prompt
from gatekeep.storage.models import RequestSample


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


async def _samples(session, prompt_name):
    """Fetch all RequestSample rows for prompt_name, for assertion convenience."""
    result = await session.execute(
        select(RequestSample).where(RequestSample.prompt_name == prompt_name)
    )
    return list(result.scalars().all())


# -- cache miss records a sample -------------------------------------------


async def test_cache_miss_records_sample_on_chat_completions(
    client, raw_key, counting_provider, session
):
    await create_prompt("p1", "You are a pirate.", session)
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "prompt_name": "p1",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    assert counting_provider.calls == 1

    samples = await _samples(session, "p1")
    assert len(samples) == 1
    assert samples[0].output_text == "pong"
    assert samples[0].input_messages == [{"role": "user", "content": "ping"}]


async def test_cache_miss_records_sample_on_messages(client, raw_key, counting_provider, session):
    await create_prompt("p2", "You are a pirate.", session)
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 50,
            "prompt_name": "p2",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    assert counting_provider.calls == 1

    samples = await _samples(session, "p2")
    assert len(samples) == 1
    assert samples[0].model == "claude-sonnet-5"
    assert samples[0].output_text == "pong"


# -- no prompt_name means no sample -----------------------------------------


async def test_cache_miss_without_prompt_name_records_no_sample(
    client, raw_key, counting_provider, session
):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "no-prompt"}]},
    )
    assert r.status_code == 200
    assert counting_provider.calls == 1

    result = await session.execute(select(RequestSample))
    assert result.scalars().all() == []


# -- cache hits never record a new sample -----------------------------------


async def test_exact_cache_hit_records_no_new_sample(client, raw_key, counting_provider, session):
    await create_prompt("p3", "You are a pirate.", session)
    body = {
        "model": "gpt-4o",
        "prompt_name": "p3",
        "messages": [{"role": "user", "content": "repeat-me"}],
    }
    await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {raw_key}"}, json=body
    )
    assert counting_provider.calls == 1
    first_count = len(await _samples(session, "p3"))
    assert first_count == 1

    r2 = await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {raw_key}"}, json=body
    )
    assert r2.status_code == 200
    assert counting_provider.calls == 1  # served from cache, provider not called again

    assert len(await _samples(session, "p3")) == first_count


async def test_semantic_cache_hit_records_no_new_sample(
    client, raw_key, counting_provider, session
):
    await create_prompt("p4", "You are a pirate.", session)
    first = {
        "model": "gpt-4o",
        "prompt_name": "p4",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
    }
    second = {
        "model": "gpt-4o",
        "prompt_name": "p4",
        "messages": [{"role": "user", "content": "What's the capital city of France?"}],
    }
    await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {raw_key}"}, json=first
    )
    assert counting_provider.calls == 1
    first_count = len(await _samples(session, "p4"))
    assert first_count == 1

    r2 = await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {raw_key}"}, json=second
    )
    assert r2.status_code == 200
    assert counting_provider.calls == 1  # semantic hit, provider not called again

    assert len(await _samples(session, "p4")) == first_count


# -- streaming bypasses sample recording -------------------------------------


async def test_streaming_request_records_no_sample(client, raw_key, counting_provider, session):
    await create_prompt("p5", "You are a pirate.", session)
    body = {
        "model": "gpt-4o",
        "stream": True,
        "prompt_name": "p5",
        "messages": [{"role": "user", "content": "stream-me"}],
    }
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    ) as r:
        [line async for line in r.aiter_lines()]

    assert counting_provider.calls == 1
    assert await _samples(session, "p5") == []


# -- provider error on a miss records no sample ------------------------------


async def test_provider_error_on_cache_miss_records_no_sample(
    client, raw_key, session, monkeypatch
):
    await create_prompt("p6", "You are a pirate.", session)

    async def _broken_complete(payload):
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(app_module._providers["anthropic"], "complete", _broken_complete)
    monkeypatch.setattr(app_module._providers["ollama"], "complete", _broken_complete)

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "prompt_name": "p6",
            "messages": [{"role": "user", "content": "will-fail"}],
        },
    )
    assert r.status_code >= 400
    assert await _samples(session, "p6") == []
