from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.embeddings import embed_text
from gatekeep.middleware.cache_semantic import (
    build_response_from_cache,
    extract_embeddable_text,
    find_semantic_match,
    store_cached_response,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, CachedResponse, RequestLog
from gatekeep.prompts import add_prompt_version, set_candidate_version
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


@pytest.fixture(autouse=True)
async def _clean_cache():
    """Flush any leftover exact-cache keys so each test starts from a clean cache.

    Scoped to this file (rather than a global conftest fixture) so tests
    that never touch caching don't pay for an eager Redis connection.
    """
    redis = get_redis()
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)


# -- extract_embeddable_text ----------------------------------------------


def _payload(**overrides):
    """Build a minimal provider-neutral payload, with overrides applied."""
    payload = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    payload.update(overrides)
    return payload


def test_extract_embeddable_text_concatenates_system_and_user():
    payload = _payload(
        system="be nice",
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
    )
    text = extract_embeddable_text(payload)
    assert "be nice" in text
    assert "hello" in text
    assert "hi there" not in text


def test_extract_embeddable_text_no_system():
    payload = _payload(messages=[{"role": "user", "content": "hello"}])
    text = extract_embeddable_text(payload)
    assert text == "hello"


def test_extract_embeddable_text_multiple_user_messages():
    payload = _payload(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
    )
    text = extract_embeddable_text(payload)
    assert "first" in text
    assert "second" in text
    assert "reply" not in text


# -- store_cached_response / find_semantic_match against real Postgres ----


async def test_store_cached_response_persists_row(session):
    embedding = embed_text("what is the capital of France?")
    await store_cached_response(
        session,
        exact_hash="hash-1",
        user_messages_text="what is the capital of France?",
        embedding=embedding,
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
    )
    row = (
        await session.execute(
            select(CachedResponse).where(CachedResponse.exact_hash == "hash-1")
        )
    ).scalar_one()
    assert row.response_text == "Paris"
    assert row.model == "claude-sonnet-5"


async def test_store_cached_response_ignores_duplicate_exact_hash(session):
    """A concurrent second insert with the same exact_hash must not raise;
    the losing write is simply skipped."""
    embedding = embed_text("what is the capital of France?")
    first = await store_cached_response(
        session,
        exact_hash="dup-hash",
        user_messages_text="what is the capital of France?",
        embedding=embedding,
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
    )
    assert first is not None

    second = await store_cached_response(
        session,
        exact_hash="dup-hash",
        user_messages_text="what is the capital of France?",
        embedding=embedding,
        response_text="Paris (from the losing request)",
        model="claude-sonnet-5",
        cost_usd=0.002,
    )
    assert second is None

    rows = (
        (
            await session.execute(
                select(CachedResponse).where(CachedResponse.exact_hash == "dup-hash")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].response_text == "Paris"


async def test_find_semantic_match_returns_none_when_empty(session):
    embedding = embed_text("anything")
    match = await find_semantic_match(
        session,
        embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
    )
    assert match is None


async def test_find_semantic_match_finds_similar_above_threshold(session):
    stored_text = "What is the capital of France?"
    await store_cached_response(
        session,
        exact_hash="hash-a",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
    )
    query_embedding = embed_text("What is the capital of France?")
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
    )
    assert match is not None
    assert match.cached.response_text == "Paris"
    assert match.similarity > 0.95


async def test_find_semantic_match_ignores_row_from_different_model(session):
    stored_text = "What is the capital of France?"
    await store_cached_response(
        session,
        exact_hash="hash-diff-model",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-haiku-4-5-20251001",
        cost_usd=0.001,
    )
    query_embedding = embed_text(stored_text)
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
    )
    assert match is None


async def test_find_semantic_match_ignores_row_from_different_prompt_version(session):
    """Correctness fix for A/B testing: a semantic-cache row generated by
    one PromptVersion (e.g. an A/B candidate) must never be served to a
    request that resolved to a *different* PromptVersion of the same
    prompt_name - even when their rendered templates are similar enough
    that a plain embedding match alone couldn't tell them apart. Requesting
    with prompt_version_num=2 must not see a row tagged prompt_version_num=1.
    """
    stored_text = "What is the capital of France?"
    await store_cached_response(
        session,
        exact_hash="hash-v1",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
        prompt_name="system-context",
        prompt_version_num=1,
    )
    query_embedding = embed_text(stored_text)
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
        prompt_version_num=2,
    )
    assert match is None


async def test_find_semantic_match_finds_row_from_same_prompt_version(session):
    stored_text = "What is the capital of France?"
    await store_cached_response(
        session,
        exact_hash="hash-v2",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
        prompt_name="system-context",
        prompt_version_num=2,
    )
    query_embedding = embed_text(stored_text)
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
        prompt_version_num=2,
    )
    assert match is not None
    assert match.cached.response_text == "Paris"


async def test_find_semantic_match_without_prompt_version_num_is_unscoped(session):
    """When the caller passes no prompt_version_num (e.g. the request had no
    prompt_name at all), behavior is unchanged from before this parameter
    existed: any matching row is considered regardless of its tag."""
    stored_text = "What is the capital of France?"
    await store_cached_response(
        session,
        exact_hash="hash-untagged",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
        prompt_name="system-context",
        prompt_version_num=1,
    )
    query_embedding = embed_text(stored_text)
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
    )
    assert match is not None


async def test_find_semantic_match_none_below_threshold(session):
    await store_cached_response(
        session,
        exact_hash="hash-b",
        user_messages_text="What is the capital of France?",
        embedding=embed_text("What is the capital of France?"),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
    )
    query_embedding = embed_text("Please write a haiku about a walrus.")
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=604800,
    )
    assert match is None


async def test_find_semantic_match_excludes_expired_rows(session):
    stored_text = "What is the capital of France?"
    row = CachedResponse(
        exact_hash="hash-c",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=1000),
    )
    session.add(row)
    await session.commit()

    query_embedding = embed_text(stored_text)
    match = await find_semantic_match(
        session,
        query_embedding,
        model="claude-sonnet-5",
        threshold=0.95,
        max_age_seconds=500,
    )
    assert match is None


# -- build_response_from_cache ---------------------------------------------


def test_build_response_from_cache_shape():
    cached = CachedResponse(
        exact_hash="h",
        user_messages_text="q",
        embedding=[0.0] * 384,
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.0,
    )
    response = build_response_from_cache(cached)
    assert response.model == "claude-sonnet-5"
    assert response.choices[0].message.content == "Paris"
    assert response.choices[0].finish_reason == "stop"


# -- wired into /v1/chat/completions ---------------------------------------


class CountingProvider:
    """A fake provider that counts completion calls to verify semantic caching."""

    def __init__(self):
        """Initialize the call counter to zero."""
        self.calls = 0

    async def complete(self, payload):
        """Record a call and return a fixed completion result."""
        self.calls += 1
        return CompletionResult(
            text="Paris is the capital of France.",
            input_tokens=5,
            output_tokens=6,
            stop_reason="end_turn",
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


async def test_semantically_similar_request_is_served_from_cache(
    client, raw_key, counting_provider
):
    body1 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
    }
    body2 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What's the capital city of France?"}],
    }
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body1,
    )
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body2,
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert counting_provider.calls == 1
    assert (
        r2.json()["choices"][0]["message"]["content"]
        == "Paris is the capital of France."
    )


async def test_semantic_hit_logs_cached_true_with_semantic_key(
    client, raw_key, session
):
    body1 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of Germany?"}],
    }
    body2 = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "What's the capital city of Germany?"}
        ],
    }
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
    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.cache_key == "semantic")
        )
    ).scalar_one()
    assert log.cached is True


async def test_dissimilar_requests_both_call_provider(
    client, raw_key, counting_provider
):
    body1 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of Spain?"}],
    }
    body2 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Please write a haiku about a cat."}],
    }
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


async def test_semantically_similar_request_against_different_model_misses_cache(
    client, raw_key, counting_provider
):
    """A cached response for one resolved model must not serve a request that
    resolves to a different model, even if the prompts are near-identical."""
    body1 = {
        "model": "gpt-4o",  # resolves to claude-sonnet-5
        "messages": [{"role": "user", "content": "What is the capital of Italy?"}],
    }
    body2 = {
        "model": "gpt-4o-mini",  # resolves to claude-haiku-4-5-20251001
        "messages": [{"role": "user", "content": "What's the capital city of Italy?"}],
    }
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body1,
    )
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body2,
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert counting_provider.calls == 2


async def test_candidate_and_active_semantic_cache_entries_do_not_cross_contaminate(
    client, raw_key, counting_provider, session
):
    """End-to-end proof of the A/B cache-correctness fix: with a candidate
    configured at 100%, a semantically-similar follow-up request must still
    call the provider fresh, rather than reusing the active version's
    cached response - and vice versa once the candidate is cleared. Without
    prompt_version_num scoping, the near-identical templates and
    near-identical user text would otherwise let one version's cached
    answer leak into the other version's response.
    """
    from gatekeep.prompts import clear_candidate_version, create_prompt

    await create_prompt("system-context", "You are a pirate.", session)
    await add_prompt_version("system-context", "You are a wizard.", session)

    body1 = {
        "model": "gpt-4o",
        "prompt_name": "system-context",
        "messages": [{"role": "user", "content": "What is the capital of Japan?"}],
    }
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body1,
    )
    assert r1.status_code == 200
    assert counting_provider.calls == 1

    # Now flip 100% of traffic to the candidate (a different template) and
    # send a semantically-similar follow-up: this must NOT reuse the
    # active version's cached answer.
    await set_candidate_version("system-context", 2, 100.0, session)
    body2 = {
        "model": "gpt-4o",
        "prompt_name": "system-context",
        "messages": [{"role": "user", "content": "What's the capital city of Japan?"}],
    }
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body2,
    )
    assert r2.status_code == 200
    assert counting_provider.calls == 2  # must NOT be served from cache

    await clear_candidate_version("system-context", session)


async def test_concurrent_identical_requests_both_succeed(
    client, raw_key, counting_provider
):
    """Two concurrent requests with identical text can both miss the caches
    and race to write the same exact_hash cache row; the client-visible
    response must still succeed for both, even though only one cache write
    wins."""
    import asyncio

    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of Portugal?"}],
    }
    r1, r2 = await asyncio.gather(
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body,
        ),
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body,
        ),
    )
    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_streaming_requests_bypass_semantic_cache(
    client, raw_key, counting_provider
):
    body = {
        "model": "gpt-4o",
        "stream": True,
        "messages": [{"role": "user", "content": "stream-me-semantic"}],
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
