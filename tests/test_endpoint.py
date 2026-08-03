import json

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.accounting import calculate_cost
from gatekeep.app import app
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.evals import create_suite
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, EvalRun, Prompt, RequestLog
from gatekeep.prompts import (
    add_prompt_version,
    create_prompt,
    get_active_prompt_version,
    set_candidate_version,
)
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


@pytest.fixture(autouse=True)
async def _clean_cache():
    """Flush exact-cache, rate-limit, and budget keys so tests in this file
    don't collide (the schema reset between tests recycles api_keys ids, so
    a new key can otherwise inherit a stale Redis bucket/counter/cache entry
    left by an earlier test's key of the same id)."""
    redis = get_redis()
    for prefix in ("cache:exact:*", "ratelimit:*", "budget:*"):
        async for key in redis.scan_iter(prefix):
            await redis.delete(key)
    yield
    for prefix in ("cache:exact:*", "ratelimit:*", "budget:*"):
        async for key in redis.scan_iter(prefix):
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

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == r.json()["id"])
        )
    ).scalar_one()
    assert log.prompt_version_num == 1


# -- A/B candidate traffic split -------------------------------------------


async def test_candidate_at_100_pct_always_serves_candidate_template(
    client, raw_key, session
):
    """A candidate configured at 100% traffic must always be served instead
    of the active version, and the served version's number must land on
    RequestLog for later active-vs-candidate comparison."""
    await create_prompt("system-context", "You are a pirate.", session)
    await add_prompt_version("system-context", "You are a wizard.", session)
    await set_candidate_version("system-context", 2, 100.0, session)

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
                "messages": [{"role": "user", "content": "ping-candidate-100"}],
            },
        )
    finally:
        app_module._providers["anthropic"].complete = original_complete

    assert r.status_code == 200
    assert captured["payload"]["system"] == "You are a wizard."

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == r.json()["id"])
        )
    ).scalar_one()
    assert log.prompt_version_num == 2


async def test_candidate_at_0_pct_never_serves_candidate_template(
    client, raw_key, session
):
    """A candidate configured at 0% traffic must behave exactly like no
    candidate at all: always the active version, never the candidate."""
    await create_prompt("system-context", "You are a pirate.", session)
    await add_prompt_version("system-context", "You are a wizard.", session)
    await set_candidate_version("system-context", 2, 0.0, session)

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
                "messages": [{"role": "user", "content": "ping-candidate-0"}],
            },
        )
    finally:
        app_module._providers["anthropic"].complete = original_complete

    assert r.status_code == 200
    assert captured["payload"]["system"] == "You are a pirate."

    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == r.json()["id"])
        )
    ).scalar_one()
    assert log.prompt_version_num == 1


async def test_candidate_split_routes_a_mix_of_active_and_candidate_requests(
    client, raw_key, session
):
    """A mid-range split (e.g. 50%) must actually produce a mix of requests
    served by the active version and requests served by the candidate,
    each correctly reflected in RequestLog.prompt_version_num - this is the
    end-to-end proof that the split reaches the real request path, not just
    the unit-level resolver."""
    await create_prompt("system-context", "You are a pirate.", session)
    await add_prompt_version("system-context", "You are a wizard.", session)
    await set_candidate_version("system-context", 2, 50.0, session)

    response_ids = []
    for i in range(40):
        r = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={
                "model": "gpt-4o",
                "prompt_name": "system-context",
                "messages": [{"role": "user", "content": f"ping-split-{i}"}],
            },
        )
        assert r.status_code == 200
        response_ids.append(r.json()["id"])

    logs = (
        (
            await session.execute(
                select(RequestLog).where(RequestLog.response_id.in_(response_ids))
            )
        )
        .scalars()
        .all()
    )
    served_versions = {log.prompt_version_num for log in logs}
    assert served_versions == {1, 2}


async def test_promote_prompt_unaffected_by_inflight_candidate_via_endpoint(
    client, raw_key, session
):
    """promote_prompt/rollback_prompt must still work exactly as before even
    with a candidate configured in-flight, proving this feature's "bigger
    blast radius" doesn't destabilize the existing binary promote/rollback
    model through the real request path."""
    from gatekeep.prompts import promote_prompt

    await create_prompt("system-context", "You are a pirate.", session)
    await add_prompt_version("system-context", "You are a wizard.", session)
    await add_prompt_version("system-context", "You are a ghost.", session)
    await set_candidate_version("system-context", 3, 20.0, session)

    await promote_prompt("system-context", 2, session)

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "prompt_name": "system-context",
            "messages": [{"role": "user", "content": "ping-promote-with-candidate"}],
        },
    )
    assert r.status_code == 200
    log = (
        await session.execute(
            select(RequestLog).where(RequestLog.response_id == r.json()["id"])
        )
    ).scalar_one()
    # candidate (v3) at 20% may or may not have fired for this single
    # request, but the active version promoted-to (v2) must be a valid
    # outcome and the candidate config must remain untouched.
    assert log.prompt_version_num in (2, 3)
    prompt_row = (
        await session.execute(select(Prompt).where(Prompt.name == "system-context"))
    ).scalar_one()
    assert prompt_row.active_version_id is not None
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 2


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


@pytest_asyncio.fixture
async def budgeted_raw_key(session):
    """A key whose monthly_budget_usd is set to half of one FakeProvider
    completion's cost, so the first request is allowed (spend starts at $0)
    but the second is rejected once the first request's full cost has been
    recorded."""
    raw = generate_key()
    one_call_cost = calculate_cost("gpt-4o", prompt_tokens=3, completion_tokens=1)
    session.add(
        ApiKey(name="b", key_hash=hash_key(raw), monthly_budget_usd=one_call_cost / 2)
    )
    await session.commit()
    return raw


async def test_budget_cap_allows_below_cap_then_rejects_once_exceeded(
    client, budgeted_raw_key
):
    """Exercise the budget cap through the real /v1/chat/completions endpoint:
    a request under the cap succeeds, and the very next request - now that
    the first request's cost has pushed cumulative spend past the cap -
    gets a real 429 with a budget-shaped error body (not just at the
    require_budget dependency level)."""
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "spend-me"}],
    }
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {budgeted_raw_key}"},
        json=body,
    )
    assert r.status_code == 200

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {budgeted_raw_key}"},
        json={**body, "messages": [{"role": "user", "content": "spend-me-again"}]},
    )
    assert r.status_code == 429
    response_body = r.json()
    assert response_body["error"]["type"] == "budget_exceeded_error"


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


async def test_non_streaming_records_latency_columns(client, raw_key, session):
    """Non-streamed requests get duration and provider time, but no TTFT."""
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.duration_ms is not None and log.duration_ms > 0
    assert log.provider_ms is not None and log.provider_ms >= 0
    assert log.duration_ms >= log.provider_ms
    assert log.ttft_ms is None


async def test_streaming_records_ttft_and_duration(client, raw_key, session):
    """Streamed requests get all three, with TTFT no later than the total."""
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
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
    # Non-strict: a fake provider yielding without awaiting can produce equal
    # values at float resolution, and a strict < would be flaky.
    assert log.ttft_ms <= log.duration_ms
    assert log.provider_ms is not None


async def test_cache_hit_leaves_provider_ms_null(client, raw_key, session):
    """A served-from-cache response made no upstream call."""
    body = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "cache-me"}],
    }
    headers = {"Authorization": f"Bearer {raw_key}"}
    await client.post("/v1/chat/completions", headers=headers, json=body)
    await client.post("/v1/chat/completions", headers=headers, json=body)

    logs = (
        (await session.execute(select(RequestLog).order_by(RequestLog.id)))
        .scalars()
        .all()
    )
    assert len(logs) == 2
    assert logs[1].cached is True
    assert logs[1].provider_ms is None
    assert logs[1].duration_ms is not None
    assert logs[1].ttft_ms is None


async def test_middleware_records_e2e_for_sse_under_the_stream_path(client, raw_key):
    """One recorder per metric: the middleware owns E2E on every path, and the
    ASGI span necessarily contains the token span it is compared against."""
    from gatekeep.observability import metrics

    def sample_for(histogram, suffix, labels):
        for sample in histogram.collect()[0].samples:
            if sample.name.endswith(suffix) and sample.labels == labels:
                return sample.value
        return 0.0

    e2e_labels = {"model": "claude-sonnet-5", "path": "stream"}
    ttlt_labels = {"model": "claude-sonnet-5"}
    before_e2e_count = sample_for(metrics.request_duration_seconds, "_count", e2e_labels)
    before_e2e_sum = sample_for(metrics.request_duration_seconds, "_sum", e2e_labels)
    before_ttlt_count = sample_for(
        metrics.time_to_last_token_seconds, "_count", ttlt_labels
    )
    before_ttlt_sum = sample_for(metrics.time_to_last_token_seconds, "_sum", ttlt_labels)

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "sse-only"}],
            "stream": True,
        },
    ) as response:
        async for _ in response.aiter_lines():
            pass

    e2e_delta = sample_for(metrics.request_duration_seconds, "_sum", e2e_labels) - (
        before_e2e_sum
    )
    ttlt_delta = sample_for(metrics.time_to_last_token_seconds, "_sum", ttlt_labels) - (
        before_ttlt_sum
    )
    assert (
        sample_for(metrics.request_duration_seconds, "_count", e2e_labels)
        == before_e2e_count + 1
    ), "exactly one E2E observation per streamed request, no double-count"
    assert (
        sample_for(metrics.time_to_last_token_seconds, "_count", ttlt_labels)
        == before_ttlt_count + 1
    )
    assert e2e_delta >= ttlt_delta, "the ASGI span contains the time-to-last-token span"
