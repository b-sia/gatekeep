import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from prometheus_client.parser import text_string_to_metric_families

import gatekeep.app as app_module
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.cache_semantic import (
    find_semantic_match,
    store_cached_response,
)
from gatekeep.middleware.ratelimit import check_rate_limit, get_redis
from gatekeep.models import ApiKey
from gatekeep.observability.metrics import observe_request
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


@pytest.fixture(autouse=True)
async def _clean_bucket():
    """Flush leftover rate-limit and exact-cache Redis keys around each test."""
    redis = get_redis()
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("ratelimit:*"):
        await redis.delete(key)
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)


# -- observe_request ---------------------------------------------------


def _metric_sample(text: str, name: str, labels: dict[str, str]) -> float | None:
    """Find one sample's value from raw Prometheus exposition text, or None."""
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                return sample.value
    return None


def test_observe_request_records_token_and_cost_histograms():
    from gatekeep.observability import metrics

    observe_request(
        model="test-model-tokens",
        key_id=999,
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.002,
    )
    output = metrics.request_tokens.collect()[0]
    total_samples = [s for s in output.samples if s.name.endswith("_sum")]
    assert any(
        s.labels.get("model") == "test-model-tokens" and s.value == 15
        for s in total_samples
    )
    cost_output = metrics.request_cost_usd.collect()[0]
    cost_samples = [s for s in cost_output.samples if s.name.endswith("_sum")]
    assert any(
        s.labels.get("model") == "test-model-tokens" and s.value == pytest.approx(0.002)
        for s in cost_samples
    )


# -- check_rate_limit wiring (via require_rate_limit dependency) --------


async def test_check_rate_limit_sets_gauge_directly():
    """check_rate_limit itself doesn't touch metrics; the gauge is set by the
    require_rate_limit dependency. This just documents the raw values used."""
    redis = get_redis()
    allowed, tokens = await check_rate_limit(
        redis, key_id=12345, capacity=3, refill_rate=0.001, now=1000.0
    )
    assert allowed is True
    assert tokens == pytest.approx(2.0)


# -- find_semantic_match now surfaces a similarity score -----------------


async def test_find_semantic_match_returns_similarity_score(session):
    from gatekeep.embeddings import embed_text

    stored_text = "What is the capital of France?"
    await store_cached_response(
        session,
        exact_hash="metrics-hash-a",
        user_messages_text=stored_text,
        embedding=embed_text(stored_text),
        response_text="Paris",
        model="claude-sonnet-5",
        cost_usd=0.001,
    )
    match = await find_semantic_match(
        session,
        embed_text(stored_text),
        model="claude-sonnet-5",
        threshold=0.5,
        max_age_seconds=604800,
    )
    assert match is not None
    assert match.cached.response_text == "Paris"
    assert match.similarity > 0.5


# -- /metrics endpoint, end-to-end via the real app ----------------------


class CountingProvider:
    """A fake provider that counts completion calls."""

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
    session.add(ApiKey(name="metrics-test", key_hash=hash_key(raw)))
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


async def test_metrics_endpoint_is_unauthenticated_and_valid(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    families = list(text_string_to_metric_families(resp.text))
    assert len(families) > 0


async def test_metrics_endpoint_reports_request_totals_after_a_completion(
    client, raw_key
):
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi there"}]}
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    assert r.status_code == 200
    resp = await client.get("/metrics")
    value = _metric_sample(
        resp.text, "gatekeep_requests_total", {"model": "claude-sonnet-5"}
    )
    assert value is not None
    assert value >= 1


async def test_metrics_endpoint_reports_rate_limit_gauge_after_a_completion(
    client, raw_key
):
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi again"}]}
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body,
    )
    resp = await client.get("/metrics")
    families = {f.name: f for f in text_string_to_metric_families(resp.text)}
    assert "gatekeep_rate_limit_remaining" in families


async def test_metrics_endpoint_reports_cache_exact_hit_and_miss(client, raw_key):
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "cache-me"}]}
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
    resp = await client.get("/metrics")
    misses = _metric_sample(
        resp.text, "gatekeep_cache_exact_misses_total", {"model": "claude-sonnet-5"}
    )
    hits = _metric_sample(
        resp.text, "gatekeep_cache_exact_hits_total", {"model": "claude-sonnet-5"}
    )
    assert misses is not None and misses >= 1
    assert hits is not None and hits >= 1


async def test_metrics_endpoint_reports_cache_semantic_hit_and_miss(client, raw_key):
    body1 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
    }
    body2 = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What's the capital city of France?"}],
    }
    body3 = {
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
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=body3,
    )
    resp = await client.get("/metrics")
    hits = _metric_sample(
        resp.text,
        "gatekeep_cache_semantic_hits_total",
        {"model": "claude-sonnet-5"},
    )
    misses = _metric_sample(
        resp.text,
        "gatekeep_cache_semantic_misses_total",
        {"model": "claude-sonnet-5"},
    )
    similarity_count = _metric_sample(
        resp.text,
        "gatekeep_cache_semantic_similarity_count",
        {"model": "claude-sonnet-5"},
    )
    cost_saved = _metric_sample(resp.text, "gatekeep_cache_cost_saved_usd_total", {})
    assert hits is not None and hits >= 1
    assert misses is not None and misses >= 1
    assert similarity_count is not None and similarity_count >= 1
    assert cost_saved is not None and cost_saved > 0


# -- latency histograms ------------------------------------------------


def test_latency_histograms_have_llm_appropriate_buckets():
    """Default prometheus buckets stop at 10s, which is useless for LLM traffic."""
    from gatekeep.observability import metrics

    assert max(metrics.LATENCY_BUCKETS_WIDE) == 120
    assert min(metrics.LATENCY_BUCKETS_WIDE) == 0.005
    assert max(metrics.LATENCY_BUCKETS_TIGHT) == 2


def test_request_duration_seconds_is_labeled_by_model_and_path():
    from gatekeep.observability import metrics

    metrics.request_duration_seconds.labels(
        model="test-latency-model", path="provider"
    ).observe(1.5)
    samples = metrics.request_duration_seconds.collect()[0].samples
    assert any(
        s.name.endswith("_sum")
        and s.labels == {"model": "test-latency-model", "path": "provider"}
        and s.value == pytest.approx(1.5)
        for s in samples
    )


def test_latency_histograms_are_not_labeled_by_key_id():
    """key_id is unbounded; per-key latency comes from Postgres instead."""
    from gatekeep.observability import metrics

    for histogram in (
        metrics.request_duration_seconds,
        metrics.provider_duration_seconds,
        metrics.gateway_overhead_seconds,
        metrics.ttft_seconds,
        metrics.inter_token_seconds,
    ):
        assert "key_id" not in histogram._labelnames


def test_single_label_histograms_take_model_only():
    from gatekeep.observability import metrics

    metrics.ttft_seconds.labels(model="test-ttft-model").observe(0.25)
    metrics.inter_token_seconds.labels(model="test-ttft-model").observe(0.01)
    metrics.provider_duration_seconds.labels(model="test-ttft-model").observe(2.0)
    samples = metrics.ttft_seconds.collect()[0].samples
    assert any(
        s.name.endswith("_count") and s.labels == {"model": "test-ttft-model"}
        for s in samples
    )
