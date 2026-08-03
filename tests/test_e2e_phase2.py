"""Full-flow Phase 2 integration tests, driven through the real FastAPI app.

Exercises the six scenarios from the Task 7 brief end-to-end against real
Postgres and Redis (a fake provider stands in for the LLM backend, matching
the pattern used by the other endpoint test files): cache miss -> logged,
exact-cache hit, semantic-cache hit, rate-limit exhaustion -> 429, the
/metrics endpoint reflecting all of the above, and prompt-update cache
invalidation.
"""

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.middleware.cache_exact import get_cached_response
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, CachedResponse, RequestLog
from gatekeep.prompts import create_prompt, promote_prompt
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


@pytest.fixture(autouse=True)
async def _clean_redis():
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


class CountingProvider:
    """A fake provider that counts completion calls and returns a fixed answer."""

    def __init__(self):
        """Initialize the call counter to zero."""
        self.calls = 0

    async def complete(self, payload):
        """Record a call and return a fixed completion result about France's capital."""
        self.calls += 1
        return CompletionResult(
            text="Paris is the capital of France.",
            input_tokens=5,
            output_tokens=6,
            stop_reason="end_turn",
        )

    async def stream(self, payload):
        """Record a call and yield a fixed stream of deltas (unused in this file)."""
        self.calls += 1
        for t in ["po", "ng"]:
            yield TextDelta(text=t)
        yield StreamEnd(stop_reason="end_turn", input_tokens=3, output_tokens=2)


@pytest_asyncio.fixture
async def raw_key(session):
    """Create and return a raw API key backed by a fresh ApiKey row."""
    raw = generate_key()
    session.add(ApiKey(name="e2e", key_hash=hash_key(raw)))
    await session.commit()
    return raw


@pytest_asyncio.fixture
async def counting_provider(monkeypatch):
    """Install a CountingProvider as both providers so tests can assert call counts."""
    fake = CountingProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", fake)
    monkeypatch.setitem(app_module._providers, "ollama", fake)
    return fake


@pytest_asyncio.fixture
async def client(counting_provider):
    """An httpx client driving the real FastAPI app in-process via ASGI transport."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _metric_sample(text: str, name: str, labels: dict[str, str]) -> float | None:
    """Find one sample's value from raw Prometheus exposition text, or None."""
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return sample.value
    return None


async def test_full_flow_miss_exact_hit_semantic_hit_ratelimit_and_metrics(
    client, raw_key, counting_provider, session
):
    """Scenarios 1, 2, 3, 4, 6: miss->logged, exact hit, semantic hit, 429, /metrics."""
    settings = get_settings()

    # -- Scenario 1: request A misses every cache and gets logged. --
    body_a = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
    }
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body_a,
    )
    assert r1.status_code == 200
    assert counting_provider.calls == 1
    response_id = r1.json()["id"]
    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == response_id)
        )
    ).scalar_one()
    assert log.cached is False

    # -- Scenario 2: request A again is served from the exact cache. --
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body_a,
    )
    assert r2.status_code == 200
    assert counting_provider.calls == 1  # no new provider call
    assert (
        r2.json()["choices"][0]["message"]["content"]
        == "Paris is the capital of France."
    )
    exact_hit_log = (
        await session.execute(
            select(RequestLog).where(RequestLog.cache_key.isnot(None))
        )
    ).scalar_one()
    assert exact_hit_log.cached is True

    # -- Scenario 3: request B, similar but not identical, hits the semantic cache. --
    body_b = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What's the capital city of France?"}],
    }
    r3 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body_b,
    )
    assert r3.status_code == 200
    assert counting_provider.calls == 1  # still no new provider call
    semantic_log = (
        await session.execute(
            select(RequestLog).where(RequestLog.cache_key == "semantic")
        )
    ).scalar_one()
    assert semantic_log.cached is True

    # -- Scenario 4: exhaust the token bucket -> 429 with Retry-After. --
    monkeypatch_settings = settings  # same cached Settings singleton used by the app
    monkeypatch_settings.rate_limit_tokens_per_min = 1
    monkeypatch_settings.rate_limit_refill_rate = 1 / 60
    try:
        body_c = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "burn the one token"}],
        }
        allowed = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body_c,
        )
        assert allowed.status_code == 200
        exhausted = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body_c,
        )
        assert exhausted.status_code == 429
        assert "retry-after" in exhausted.headers
        assert int(exhausted.headers["retry-after"]) >= 1
    finally:
        monkeypatch_settings.rate_limit_tokens_per_min = 100
        monkeypatch_settings.rate_limit_refill_rate = 100 / 60

    # -- Scenario 6: /metrics reflects all of the above in valid Prometheus format. --
    metrics_resp = await client.get("/metrics")
    assert metrics_resp.status_code == 200
    families = list(text_string_to_metric_families(metrics_resp.text))
    assert len(families) > 0

    exact_hits = _metric_sample(
        metrics_resp.text,
        "gatekeep_cache_exact_hits_total",
        {"model": "claude-sonnet-5"},
    )
    semantic_hits = _metric_sample(
        metrics_resp.text,
        "gatekeep_cache_semantic_hits_total",
        {"model": "claude-sonnet-5"},
    )
    requests_total = _metric_sample(
        metrics_resp.text, "gatekeep_requests_total", {"model": "claude-sonnet-5"}
    )
    rate_limit_rejections = _metric_sample(
        metrics_resp.text, "gatekeep_rate_limit_rejections_total", {}
    )
    assert exact_hits is not None and exact_hits >= 1
    assert semantic_hits is not None and semantic_hits >= 1
    assert requests_total is not None and requests_total >= 1
    assert rate_limit_rejections is not None and rate_limit_rejections >= 1


async def test_prompt_update_invalidates_cache(client, raw_key, session):
    """Scenario 5: promoting a new prompt version clears the stale cached response.

    Creates a prompt, makes a request tagged with prompt_name that gets
    cached, promotes a new version of that prompt, then repeats the exact
    same request and confirms it's a fresh cache miss (a new provider call),
    not the stale response served from the now-invalidated cache.
    """
    await create_prompt("greeting", "You are a formal assistant.", session)

    class RecordingProvider:
        """A fake provider that records every payload's resolved system text."""

        def __init__(self):
            """Initialize the call counter and the list of captured system texts."""
            self.calls = 0
            self.systems = []

        async def complete(self, payload):
            """Record the call's system text and return a fixed completion."""
            self.calls += 1
            self.systems.append(payload.get("system"))
            return CompletionResult(
                text="Hello.", input_tokens=4, output_tokens=2, stop_reason="end_turn"
            )

        async def stream(self, payload):
            """Unused by this test; present only to satisfy the provider protocol."""
            yield TextDelta(text="x")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)

    provider = RecordingProvider()
    app_module._providers["anthropic"] = provider
    app_module._providers["ollama"] = provider

    body = {
        "model": "gpt-4o",
        "prompt_name": "greeting",
        "messages": [{"role": "user", "content": "hi"}],
    }

    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r1.status_code == 200
    assert provider.calls == 1

    # -- Directly verify the real HTTP write tagged both caches with "greeting". --
    redis = get_redis()
    by_prompt_key = "cache:exact:by-prompt:greeting"
    tagged_hashes = await redis.smembers(by_prompt_key)
    assert tagged_hashes, "expected the exact-cache write to tag a hash under greeting"
    for h in tagged_hashes:
        assert await get_cached_response(redis, h) is not None

    tagged_rows = (
        (
            await session.execute(
                select(CachedResponse).where(CachedResponse.prompt_name == "greeting")
            )
        )
        .scalars()
        .all()
    )
    assert tagged_rows, "expected the semantic-cache write to tag a row with greeting"

    # Repeating the same request now hits the exact cache tagged with "greeting".
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r2.status_code == 200
    assert provider.calls == 1  # served from cache, no new provider call

    # Promoting a new version invalidates every cache entry tagged "greeting".
    from gatekeep.prompts import add_prompt_version

    await add_prompt_version("greeting", "You are a casual assistant.", session)
    await promote_prompt("greeting", 2, session, redis=redis)

    # -- Directly verify invalidation cleared both caches' tagged entries. --
    assert await redis.smembers(by_prompt_key) == set()
    for h in tagged_hashes:
        assert await get_cached_response(redis, h) is None

    remaining_rows = (
        (
            await session.execute(
                select(CachedResponse).where(CachedResponse.prompt_name == "greeting")
            )
        )
        .scalars()
        .all()
    )
    assert remaining_rows == []

    r3 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r3.status_code == 200
    assert provider.calls == 2  # fresh miss, not served from the stale cache
    assert provider.systems[-1] == "You are a casual assistant."
