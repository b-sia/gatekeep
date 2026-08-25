import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

import gatekeep.app as app_module
from gatekeep.accounts.accounting import calculate_cost
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.app import app
from gatekeep.config import get_settings
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.prompts.evals import create_suite
from gatekeep.prompts.prompts import (
    add_prompt_version,
    create_prompt,
    get_active_prompt_version,
    set_candidate_version,
)
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta
from gatekeep.storage.models import ApiKey, EvalRun, Prompt, RequestLog
from tests.helpers import create_account


async def test_run_shielded_completes_the_coroutine_despite_repeated_cancellation():
    """A disconnecting client can inject cancellation into the SSE generator's
    `finally` block more than once (e.g. a persistent cancel scope). The
    accounting write there must run to completion regardless.

    `_run_shielded` absorbs the outer cancellations rather than letting them
    cut the DB commit short - it does NOT re-raise CancelledError to its
    caller for an outer cancellation (only if the wrapped coroutine's own
    task is itself done/cancelled, which never happens here). In the real
    generator, the CancelledError the client disconnect caused is already
    propagating via the `except ... raise` clause that ran before `finally`;
    this helper's job is only to keep the write from being cut short while
    that propagation is paused, not to re-signal the cancellation itself.
    So `runner()` below completes normally even though its task was
    cancelled twice - that is the correct, intended behavior."""
    completed = False

    async def slow_write():
        nonlocal completed
        await asyncio.sleep(0.05)
        completed = True

    async def runner():
        await app_module._run_shielded(slow_write())

    task = asyncio.ensure_future(runner())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # cancel again while the shielded write is still in flight
    await task  # must NOT raise: both cancellations are absorbed until the write finishes
    assert completed, "the shielded write must run to completion despite repeated cancellation"


async def test_run_shielded_returns_the_coroutines_result_when_not_cancelled():
    async def compute():
        return 42

    result = await app_module._run_shielded(compute())
    assert result == 42


def sample_for(histogram, suffix, labels):
    """Return a histogram's `_sum`/`_count` sample value for an exact label
    set, or 0.0 if that label combination hasn't been observed yet."""
    for sample in histogram.collect()[0].samples:
        if sample.name.endswith(suffix) and sample.labels == labels:
            return sample.value
    return 0.0


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


class BrokenProvider:
    """Raises before ever completing, so provider_ms never gets published -
    `mark(request, path="provider")` still runs first (app.py:487)."""

    async def complete(self, payload):
        raise RuntimeError("upstream exploded")

    async def stream(self, payload):
        raise RuntimeError("upstream exploded")
        yield  # pragma: no cover - unreachable, makes this an async generator


class MidStreamFailureProvider:
    """Yields some deltas, then raises - reproduces issue #17's "provider
    raises mid-stream" case, as opposed to BrokenProvider which never
    yields anything."""

    async def complete(self, payload):
        raise RuntimeError("upstream exploded mid non-stream")

    async def stream(self, payload):
        yield TextDelta(text="po")
        yield TextDelta(text="ng")
        raise RuntimeError("upstream exploded mid-stream")


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


class StreamEndThenRaisesProvider:
    """Yields a StreamEnd carrying authoritative token counts, then keeps
    going and raises. Reproduces a provider whose generator emits more after
    the terminal event; the _sse loop must stop at StreamEnd so the trailing
    error cannot re-tag an already-completed stream as failed or clobber its
    authoritative counts with estimates."""

    async def complete(self, payload):
        raise RuntimeError("not used")

    async def stream(self, payload):
        yield TextDelta(text="po")
        yield StreamEnd(stop_reason="end_turn", input_tokens=3, output_tokens=2)
        raise RuntimeError("provider yielded past StreamEnd")


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


@pytest_asyncio.fixture
async def broken_client(monkeypatch):
    broken = BrokenProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", broken)
    monkeypatch.setitem(app_module._providers, "ollama", broken)
    monkeypatch.setitem(app_module._providers, "openai", broken)
    monkeypatch.setitem(app_module._providers, "google", broken)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def mid_stream_failure_client(monkeypatch):
    failing = MidStreamFailureProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", failing)
    monkeypatch.setitem(app_module._providers, "ollama", failing)
    monkeypatch.setitem(app_module._providers, "openai", failing)
    monkeypatch.setitem(app_module._providers, "google", failing)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def stream_ends_without_marker_client(monkeypatch):
    stubbed = StreamEndsWithoutMarkerProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", stubbed)
    monkeypatch.setitem(app_module._providers, "ollama", stubbed)
    monkeypatch.setitem(app_module._providers, "openai", stubbed)
    monkeypatch.setitem(app_module._providers, "google", stubbed)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def stream_end_then_raises_client(monkeypatch):
    stubbed = StreamEndThenRaisesProvider()
    monkeypatch.setitem(app_module._providers, "anthropic", stubbed)
    monkeypatch.setitem(app_module._providers, "ollama", stubbed)
    monkeypatch.setitem(app_module._providers, "openai", stubbed)
    monkeypatch.setitem(app_module._providers, "google", stubbed)
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


async def test_openai_prefixed_model_routes_to_openai_provider_response(client, raw_key):
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


async def test_unpriced_paid_model_is_rejected_before_the_provider_call(client, raw_key, session):
    """The default `pricing_miss_policy` is "reject": a billed-provider model with
    no configured price is refused with a 400 before any upstream call (issue
    #25's fail-open hole), rather than served and billed at $0. `openai/` routes
    straight to the openai provider, so an unknown model there is genuinely
    unpriced."""
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "openai/gpt-nonexistent-9000",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    # Refused before the provider ran, so no RequestLog row was written.
    assert (await session.execute(select(RequestLog))).first() is None


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
        await session.execute(select(RequestLog).where(RequestLog.response_id == response_id))
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == response_id))
    ).scalar_one()
    assert log.prompt_tokens == 3
    assert log.completion_tokens == 2
    assert log.total_tokens == 5


async def test_prompt_name_substitutes_active_template_as_system_message(client, raw_key, session):
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == r.json()["id"]))
    ).scalar_one()
    assert log.prompt_version_num == 1


# -- A/B candidate traffic split -------------------------------------------


async def test_candidate_at_100_pct_always_serves_candidate_template(client, raw_key, session):
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == r.json()["id"]))
    ).scalar_one()
    assert log.prompt_version_num == 2


async def test_candidate_at_0_pct_never_serves_candidate_template(client, raw_key, session):
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == r.json()["id"]))
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
        (await session.execute(select(RequestLog).where(RequestLog.response_id.in_(response_ids))))
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
    from gatekeep.prompts.prompts import promote_prompt

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
        await session.execute(select(RequestLog).where(RequestLog.response_id == r.json()["id"]))
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


async def test_promote_prompt_invalidates_cached_response_via_endpoint(client, raw_key, session):
    """Promoting a new prompt version must invalidate cache entries tagged
    with that prompt: a request cached under the old system text must miss
    and re-hit the provider with the new template instead of keep serving
    the stale cached output. Mechanism-level cache-tag invalidation is
    covered directly in tests/prompts/test_prompts.py; this proves it fires
    through the real request path."""
    from gatekeep.prompts.prompts import promote_prompt

    await create_prompt("greeting", "You are a formal assistant.", session)

    calls = []
    original_complete = app_module._providers["anthropic"].complete

    async def recording_complete(payload):
        calls.append(payload.get("system"))
        return await original_complete(payload)

    app_module._providers["anthropic"].complete = recording_complete
    try:
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
        assert len(calls) == 1

        # Repeating the same request is served from the exact cache.
        r2 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body,
        )
        assert r2.status_code == 200
        assert len(calls) == 1  # no new provider call

        await add_prompt_version("greeting", "You are a casual assistant.", session)
        await promote_prompt("greeting", 2, session, redis=get_redis())

        # Repeating the same request again is now a fresh miss with the new
        # template, not the stale response served from the invalidated cache.
        r3 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json=body,
        )
        assert r3.status_code == 200
        assert len(calls) == 2
        assert calls[-1] == "You are a casual assistant."
    finally:
        app_module._providers["anthropic"].complete = original_complete


async def test_rate_limit_exhaustion_returns_429_with_retry_after(client, raw_key, monkeypatch):
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
    """A key on an account whose monthly_budget_usd is half of one FakeProvider
    completion's cost, so the first request is allowed (spend starts at $0)
    but the second is rejected once the first request's full cost has been
    recorded. Budget is pooled at the account."""
    raw = generate_key()
    one_call_cost = calculate_cost("openai", "gpt-4o", prompt_tokens=3, completion_tokens=1)
    account = await create_account(session, monthly_budget_usd=one_call_cost / 2)
    session.add(ApiKey(name="b", key_hash=hash_key(raw), account_id=account.id))
    await session.commit()
    return raw


async def test_budget_cap_allows_below_cap_then_rejects_once_exceeded(client, budgeted_raw_key):
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == body["id"]))
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == body["id"]))
    ).scalar_one()
    assert log.model == "claude-sonnet-5"
    assert log.routed_from is None


async def test_route_by_cost_defaults_to_false_and_never_substitutes(client, raw_key, session):
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
        await session.execute(select(RequestLog).where(RequestLog.response_id == body["id"]))
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

    logs = (await session.execute(select(RequestLog).order_by(RequestLog.id))).scalars().all()
    assert len(logs) == 2
    assert logs[1].cached is True
    assert logs[1].provider_ms is None
    assert logs[1].duration_ms is not None
    assert logs[1].ttft_ms is None


async def test_middleware_records_e2e_for_sse_under_the_stream_path(client, raw_key):
    """One recorder per metric: the middleware owns E2E on every path, and the
    ASGI span necessarily contains the token span it is compared against."""
    from gatekeep.observability import metrics

    e2e_labels = {"model": "claude-sonnet-5", "path": "stream"}
    ttlt_labels = {"model": "claude-sonnet-5"}
    overhead_labels = {"model": "claude-sonnet-5", "path": "stream"}
    before_e2e_count = sample_for(metrics.request_duration_seconds, "_count", e2e_labels)
    before_e2e_sum = sample_for(metrics.request_duration_seconds, "_sum", e2e_labels)
    before_ttlt_count = sample_for(metrics.time_to_last_token_seconds, "_count", ttlt_labels)
    before_ttlt_sum = sample_for(metrics.time_to_last_token_seconds, "_sum", ttlt_labels)
    before_overhead_count = sample_for(metrics.gateway_overhead_seconds, "_count", overhead_labels)
    before_overhead_sum = sample_for(metrics.gateway_overhead_seconds, "_sum", overhead_labels)
    before_provider_sum = sample_for(metrics.provider_duration_seconds, "_sum", ttlt_labels)

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

    e2e_delta = sample_for(metrics.request_duration_seconds, "_sum", e2e_labels) - (before_e2e_sum)
    ttlt_delta = sample_for(metrics.time_to_last_token_seconds, "_sum", ttlt_labels) - (
        before_ttlt_sum
    )
    overhead_delta = (
        sample_for(metrics.gateway_overhead_seconds, "_sum", overhead_labels) - before_overhead_sum
    )
    provider_delta = (
        sample_for(metrics.provider_duration_seconds, "_sum", ttlt_labels) - before_provider_sum
    )
    assert (
        sample_for(metrics.request_duration_seconds, "_count", e2e_labels) == before_e2e_count + 1
    ), "exactly one E2E observation per streamed request, no double-count"
    assert (
        sample_for(metrics.time_to_last_token_seconds, "_count", ttlt_labels)
        == before_ttlt_count + 1
    )
    assert e2e_delta >= ttlt_delta, "the ASGI span contains the time-to-last-token span"
    assert (
        sample_for(metrics.gateway_overhead_seconds, "_count", overhead_labels)
        == before_overhead_count + 1
    ), "exactly one overhead observation per streamed request, no double-count"
    assert overhead_delta == pytest.approx(e2e_delta - provider_delta, rel=1e-3), (
        "overhead is derived from the same E2E span as request_duration_seconds, "
        "so it must equal duration minus provider exactly on the stream path"
    )


async def test_middleware_overhead_is_exact_on_the_non_streaming_provider_path(client, raw_key):
    """The non-streaming path went through the same refactor as streaming -
    observe_non_streaming now only publishes provider_ms, the middleware
    derives overhead - but only the streaming path had an equivalent exact-
    equality check. This closes that gap for the plain provider path."""
    from gatekeep.observability import metrics

    labels = {"model": "claude-sonnet-5", "path": "provider"}
    before_duration_sum = sample_for(metrics.request_duration_seconds, "_sum", labels)
    before_overhead_sum = sample_for(metrics.gateway_overhead_seconds, "_sum", labels)
    before_provider_sum = sample_for(
        metrics.provider_duration_seconds, "_sum", {"model": "claude-sonnet-5"}
    )

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "non-streaming-exactness"}],
        },
    )
    assert r.status_code == 200

    duration_delta = (
        sample_for(metrics.request_duration_seconds, "_sum", labels) - before_duration_sum
    )
    overhead_delta = (
        sample_for(metrics.gateway_overhead_seconds, "_sum", labels) - before_overhead_sum
    )
    provider_delta = (
        sample_for(metrics.provider_duration_seconds, "_sum", {"model": "claude-sonnet-5"})
        - before_provider_sum
    )
    assert overhead_delta == pytest.approx(duration_delta - provider_delta, rel=1e-3), (
        "overhead must equal duration minus provider exactly on the non-streaming "
        "provider path too, not just on the stream path"
    )


async def test_provider_error_now_publishes_provider_ms_and_counts_overhead(
    broken_client, raw_key, session
):
    """Companion fix to issue #17's milder non-streaming case: `mark(request,
    path="provider")` already ran before `provider.complete(...)` so a failed
    call carries labels, but provider_ms was never published, so the
    middleware skipped the overhead observation entirely (see
    test_provider_error_does_not_count_whole_span_as_overhead in git history
    for the old, now-superseded behavior). The fix publishes provider_ms even
    on failure and logs a RequestLog row, so overhead is now observed and a
    row exists with outcome='provider_error'."""
    from gatekeep.observability import metrics

    labels = {"model": "claude-sonnet-5", "path": "provider"}
    before_duration_count = sample_for(metrics.request_duration_seconds, "_count", labels)
    before_overhead_count = sample_for(metrics.gateway_overhead_seconds, "_count", labels)

    r = await broken_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "provider-error"}],
        },
    )
    assert r.status_code == 502

    assert (
        sample_for(metrics.request_duration_seconds, "_count", labels) == before_duration_count + 1
    )
    assert (
        sample_for(metrics.gateway_overhead_seconds, "_count", labels) == before_overhead_count + 1
    ), "provider_ms is now published even on failure, so overhead must be observed"

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "provider_error"
    assert log.prompt_tokens == 0
    assert log.completion_tokens == 0
    assert log.cost_usd == 0
    assert log.provider_ms is not None
    assert log.path == "provider"


async def test_provider_error_does_not_skew_provider_latency_histogram(
    broken_client, raw_key, session
):
    """A failed non-streaming call publishes provider_ms for overhead
    attribution but must not enter the provider_duration_seconds histogram:
    a failed (fast-erroring or timing-out) call's duration is not 'how long a
    normal request takes', matching the exclusion of failed rows from the DB
    latency percentiles."""
    from gatekeep.observability import metrics

    labels = {"model": "claude-sonnet-5"}
    before = sample_for(metrics.provider_duration_seconds, "_count", labels)

    r = await broken_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "provider-error"}],
        },
    )
    assert r.status_code == 502

    assert sample_for(metrics.provider_duration_seconds, "_count", labels) == before, (
        "failed provider call must not be observed into the provider-latency histogram"
    )


async def test_provider_error_survives_failing_accounting_write(
    broken_client, raw_key, monkeypatch
):
    """If the accounting write in the failure path itself raises (DB down -
    often the same outage that failed the provider call), the mapped provider
    error must still reach the client. The accounting failure is logged and
    swallowed, not allowed to surface as an uncaught 500."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("db connection reset during accounting write")

    monkeypatch.setattr(app_module, "log_request", _boom)

    r = await broken_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "provider-error"}],
        },
    )
    assert r.status_code == 502, "the mapped provider error, not a masked 500"


async def test_non_streaming_records_path_matching_the_metric_label(client, raw_key, session):
    """A provider-served non-streaming request must record `path ==
    "provider"` on the `RequestLog` row `_finish_request` writes."""
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
    assert log.path == "provider"


async def test_cache_hit_records_cache_exact_path(client, raw_key, session):
    body = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "path-cache-me"}],
    }
    headers = {"Authorization": f"Bearer {raw_key}"}
    await client.post("/v1/chat/completions", headers=headers, json=body)
    await client.post("/v1/chat/completions", headers=headers, json=body)

    logs = (await session.execute(select(RequestLog).order_by(RequestLog.id))).scalars().all()
    assert [log.path for log in logs] == ["provider", "cache_exact"]


async def test_streaming_records_stream_path(client, raw_key, session):
    """A streamed request must record `path == "stream"` on the `RequestLog`
    row `_messages_sse`/`_sse` write, and the Prometheus histogram must
    record an observation under that same label - the two sinks are written
    from separate functions on this path, so both sides of the invariant
    need checking."""
    from gatekeep.observability import metrics

    before_count = sample_for(
        metrics.request_duration_seconds,
        "_count",
        {"model": "claude-sonnet-5", "path": app_module._STREAM_PATH},
    )

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
    assert log.path == "stream"
    assert log.outcome == "ok"

    after_count = sample_for(
        metrics.request_duration_seconds,
        "_count",
        {"model": "claude-sonnet-5", "path": log.path},
    )
    assert after_count > before_count


async def test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens(
    mid_stream_failure_client, raw_key, session
):
    """Reproduces issue #17's first case: a provider that raises after
    yielding some text. Before the fix, no RequestLog row is written at all
    and the budget counter never decrements."""
    from gatekeep.middleware.budget import _current_period, _spend_redis_key
    from gatekeep.middleware.ratelimit import get_redis

    async with mid_stream_failure_client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        body = "".join([line async for line in r.aiter_lines()])
    assert "upstream_error" in body
    assert "[DONE]" in body

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "provider_error"
    # "po" + "ng" = "pong", 4 chars -> ceil(4/4) = 1 estimated completion token.
    assert log.completion_tokens == 1
    assert log.prompt_tokens > 0
    assert log.cost_usd > 0
    # duration_ms is time-to-last-token (the "po"/"ng" deltas), not the
    # failure moment - see StreamTimer.finish(succeeded=False).
    assert log.duration_ms is not None
    assert log.provider_ms is not None

    key_id_row = (await session.execute(select(ApiKey.id).where(ApiKey.name == "c"))).scalar_one()
    redis = get_redis()
    spend_key = _spend_redis_key(key_id_row, _current_period())
    spent = await redis.get(spend_key)
    assert spent is not None and float(spent) > 0, (
        "record_spend must have run for the failed row, decrementing the budget"
    )


async def test_provider_error_mid_stream_observes_gateway_overhead(
    mid_stream_failure_client, raw_key
):
    """A failed stream must still publish provider_ms so the middleware's
    gateway_overhead_seconds observation isn't skipped (the observability
    drift half of issue #17)."""
    from gatekeep.observability import metrics

    labels = {"model": "claude-sonnet-5", "path": "stream"}
    before = sample_for(metrics.gateway_overhead_seconds, "_count", labels)

    async with mid_stream_failure_client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        async for _ in r.aiter_lines():
            pass

    after = sample_for(metrics.gateway_overhead_seconds, "_count", labels)
    assert after == before + 1


async def test_stream_ending_without_streamend_marker_logs_ok_with_estimates(
    stream_ends_without_marker_client, raw_key, session
):
    """A provider whose stream() completes without ever yielding StreamEnd is
    a success, not a failure: the client received the full body. The row is
    logged outcome='ok' with estimated tokens (no authoritative count exists),
    the stream ends cleanly with a synthesized terminal chunk and [DONE], and
    no phantom error event is surfaced to the client."""
    async with stream_ends_without_marker_client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        body = "".join([line async for line in r.aiter_lines()])
    assert "upstream_error" not in body
    # the full completion still reached the client (deltas "po" + "ng")
    assert '"content":"po"' in body
    assert '"content":"ng"' in body
    assert '"finish_reason":"stop"' in body
    assert "[DONE]" in body

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "ok"
    assert log.completion_tokens == 1
    assert log.cost_usd > 0


async def test_stream_error_after_streamend_does_not_overwrite_ok_row(
    stream_end_then_raises_client, raw_key, session
):
    """A provider that raises *after* yielding StreamEnd must not have its
    completed, authoritatively-counted row re-tagged provider_error: the _sse
    loop breaks at StreamEnd, so the trailing error never reaches the handler.
    The row keeps outcome='ok' and the provider's authoritative token counts
    (3/2), not estimates, and no error event is surfaced to the client."""
    async with stream_end_then_raises_client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        body = "".join([line async for line in r.aiter_lines()])
    assert "upstream_error" not in body
    assert "[DONE]" in body

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "ok"
    assert log.prompt_tokens == 3
    assert log.completion_tokens == 2


async def test_client_disconnect_mid_stream_logs_failed_row(session, raw_key):
    """Reproduces issue #17's second case: the generator receives
    CancelledError, not an Exception subclass, so the pre-fix `except
    Exception` handler never runs. Drives _sse directly rather than through
    an HTTP client: simulating a genuine client disconnect through
    httpx's ASGITransport is not reliable, and the design spec's own
    reproduction sketch calls for driving the generator directly."""
    import time as time_module

    account = await create_account(session)
    key = ApiKey(name="disconnect-test", key_hash=hash_key(generate_key()), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time_module.perf_counter()}
    gen = app_module._sse(
        FakeProvider(),
        "anthropic",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
        account_id=account.id,
        state=state,
    )
    await gen.__anext__()  # role chunk
    await gen.__anext__()  # first text delta, "po"

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "client_disconnect"
    # Only "po" was accumulated before the cancellation.
    assert log.completion_tokens == 1
    assert log.prompt_tokens > 0
    assert log.duration_ms is not None
    assert log.provider_ms is not None


async def test_client_disconnect_via_aclose_logs_failed_row(session, raw_key):
    """Real client disconnects are delivered via Starlette's aclose() -
    GeneratorExit thrown at the generator's suspended yield - not a
    directly-injected CancelledError. The athrow-based tests above cover
    the exception TYPE handling but not this delivery MECHANISM. aclose()
    must return normally (the generator catches and re-raises GeneratorExit,
    which is the successful-close case per the async generator protocol,
    not an error) and the row must still be written."""
    import time as time_module

    account = await create_account(session)
    key = ApiKey(
        name="aclose-disconnect-test",
        key_hash=hash_key(generate_key()),
        account_id=account.id,
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time_module.perf_counter()}
    gen = app_module._sse(
        FakeProvider(),
        "anthropic",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
        account_id=account.id,
        state=state,
    )
    await gen.__anext__()  # role chunk
    await gen.__anext__()  # first text delta, "po"

    await gen.aclose()  # must return normally, not raise

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "client_disconnect"
    assert log.completion_tokens == 1


async def test_client_disconnect_before_first_token_has_null_duration(session, raw_key):
    """Spec item 3: a failure before any delta arrives leaves duration_ms
    and ttft_ms null, but the row still gets written with the right
    outcome."""
    import time as time_module

    account = await create_account(session)
    key = ApiKey(
        name="disconnect-early-test",
        key_hash=hash_key(generate_key()),
        account_id=account.id,
    )
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time_module.perf_counter()}
    gen = app_module._sse(
        FakeProvider(),
        "anthropic",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
        account_id=account.id,
        state=state,
    )
    await gen.__anext__()  # role chunk only - no delta consumed yet

    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "client_disconnect"
    assert log.completion_tokens == 0
    assert log.duration_ms is None
    assert log.ttft_ms is None
