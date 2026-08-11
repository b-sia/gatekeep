## Task 5: Restructure `_sse` (OpenAI-shaped streaming) for full-path accounting

**Files:**
- Modify: `gatekeep/app.py`
- Test: `tests/test_endpoint.py`

**Interfaces:**
- Consumes: `estimate_tokens` (`gatekeep.accounting`, Task 1), `outcome` param on `log_request` (Task 2), `StreamTimer.finish(succeeded=...)` (Task 3), `_run_shielded` (Task 4).
- Produces: no new public interface - `_sse`'s external behavior (SSE body shape, `RequestLog` row on the happy path) is unchanged; this task adds behavior on the two previously-silent exit paths.

- [ ] **Step 1: Write the failing reproduction tests**

Add to `tests/test_endpoint.py`. First add a provider stub that fails mid-stream, next to the existing `BrokenProvider`:

```python
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
```

Add a fixture for it, next to `broken_client`:

```python
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
```

Add the tests (at the end of the file):

```python
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

    key_id_row = (
        await session.execute(select(ApiKey.id).where(ApiKey.name == "c"))
    ).scalar_one()
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


async def test_client_disconnect_mid_stream_logs_failed_row(session, raw_key):
    """Reproduces issue #17's second case: the generator receives
    CancelledError, not an Exception subclass, so the pre-fix `except
    Exception` handler never runs. Drives _sse directly rather than through
    an HTTP client: simulating a genuine client disconnect through
    httpx's ASGITransport is not reliable, and the design spec's own
    reproduction sketch calls for driving the generator directly."""
    import time as time_module

    key = ApiKey(name="disconnect-test", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time_module.perf_counter()}
    gen = app_module._sse(
        FakeProvider(),
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
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


async def test_client_disconnect_before_first_token_has_null_duration(session, raw_key):
    """Spec item 3: a failure before any delta arrives leaves duration_ms
    and ttft_ms null, but the row still gets written with the right
    outcome."""
    import time as time_module

    key = ApiKey(name="disconnect-early-test", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()
    await session.refresh(key)

    state = {"started_at": time_module.perf_counter()}
    gen = app_module._sse(
        FakeProvider(),
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
        "claude-sonnet-5",
        key_id=key.id,
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py -v -k "mid_stream or disconnect"`
Expected: FAIL - the provider-error tests fail on the `log = (...).scalar_one()` line with `NoResultFound` (no row written today); the disconnect tests fail the same way, or with an unhandled exception surfacing from `_sse` since it doesn't currently guard `except (GeneratorExit, asyncio.CancelledError)` at all (a raw `CancelledError` propagates immediately with no accounting).

- [ ] **Step 3: Restructure `_sse` in `gatekeep/app.py`**

Replace the entire `_sse` function body:

```python
async def _sse(
    provider: _GatewayProvider,
    payload: dict,
    model: str,
    *,
    key_id: int,
    prompt_name: str | None = None,
    routed_from: str | None = None,
    prompt_version_num: int | None = None,
    state: dict | None = None,
):
    """Stream a chat completion as OpenAI-style Server-Sent Events.

    Emits a role chunk, then a text chunk per delta, then a final chunk
    carrying the finish_reason. Accounting (`StreamTimer.finish`,
    `log_request`, `observe_request`) runs in a `finally` block so it fires
    on every exit path, not just a clean `StreamEnd`:

    - A clean finish (`StreamEnd` reached) logs `outcome="ok"` with the
      provider's authoritative token counts.
    - A provider error mid-stream surfaces as an in-band SSE error event
      (as before) and logs `outcome="provider_error"` with tokens estimated
      from the accumulated delta text via `estimate_tokens`, since no
      authoritative count exists without a `StreamEnd`.
    - A client disconnect (`GeneratorExit`/`asyncio.CancelledError`, neither
      an `Exception` subclass) logs `outcome="client_disconnect"` the same
      estimated way, then re-raises - a disconnected client cannot receive
      the closing chunk or an error event either way, so the fix records the
      row without attempting to resurrect the connection.

    Uses its own DB session (`SessionLocal`) since this generator keeps
    running after the request-scoped session dependency has already been
    closed. The accounting write in `finally` is wrapped in `_run_shielded`
    because a disconnecting client can inject cancellation more than once
    while that write is in flight, and a bare `await` there could let a
    second cancellation cut the DB commit short.

    `state` is `request.scope["state"]`, passed in because the generator runs
    after the endpoint has returned and can no longer reach the `request`
    object itself. It doubles as the channel back to the middleware:
    `StreamTimer.finish()` writes `provider_ms` onto it so the middleware can
    derive `gateway_overhead_seconds` once the stream closes, on every exit
    path including a failed one. The middleware still records end-to-end for
    this request; what the generator adds is TTFT, inter-token gaps, and
    time-to-last-token. Timing is recorded via StreamTimer and lands on the
    same RequestLog row.
    """
    completion_id = new_completion_id()
    created = int(time.time())
    timer = StreamTimer(state, model=model)
    yield _event(role_chunk(id=completion_id, created=created, model=model))

    outcome = "ok"
    input_tokens = output_tokens = 0
    accumulated: list[str] = []
    try:
        timer.provider_started()
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                timer.delta()
                accumulated.append(ev.text)
                yield _event(
                    text_chunk(ev.text, id=completion_id, created=created, model=model)
                )
            elif isinstance(ev, StreamEnd):
                outcome = "ok"
                input_tokens, output_tokens = ev.input_tokens, ev.output_tokens
                yield _event(
                    final_chunk(
                        ev.stop_reason, id=completion_id, created=created, model=model
                    )
                )
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "client_disconnect"
        input_tokens = estimate_tokens(_payload_text(payload))
        output_tokens = estimate_tokens("".join(accumulated))
        raise
    except Exception as exc:  # surface upstream errors inside the stream
        outcome = "provider_error"
        input_tokens = estimate_tokens(_payload_text(payload))
        output_tokens = estimate_tokens("".join(accumulated))
        error_payload = {
            "error": {
                "message": str(exc),
                "type": "upstream_error",
                "code": "provider_error",
            }
        }
        yield f"data: {json.dumps(error_payload)}\n\n"
    finally:
        # NEVER yield here - illegal during GeneratorExit.
        timings = timer.finish(succeeded=(outcome == "ok"))
        cost_usd = calculate_cost(model, input_tokens, output_tokens)

        async def _record() -> None:
            async with SessionLocal() as session:
                await log_request(
                    session,
                    key_id=key_id,
                    model=model,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    response_id=completion_id,
                    prompt_name=prompt_name,
                    routed_from=routed_from,
                    prompt_version_num=prompt_version_num,
                    path=_STREAM_PATH,
                    outcome=outcome,
                    duration_ms=timings.duration_ms,
                    provider_ms=timings.provider_ms,
                    ttft_ms=timings.ttft_ms,
                )
            observe_request(
                model=model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                cost_usd=cost_usd,
            )

        await _run_shielded(_record())
    # Unreachable on the client-disconnect path: the `raise` above
    # re-propagates once `finally` completes, so control never reaches here.
    yield "data: [DONE]\n\n"
```

Add the `_payload_text` helper next to `_run_shielded` (after it, before `_sse`):

```python
def _payload_text(payload: dict) -> str:
    """Concatenate a provider-neutral payload's system and message text.

    Used to estimate input tokens on a failed/aborted stream, where no
    authoritative provider-reported count exists (see `estimate_tokens`).
    `payload["messages"]` entries are always `{"role": ..., "content": str}`
    by the time they reach here (openai_to_payload/messages_to_payload have
    already flattened multimodal content to plain text).

    Args:
        payload: The provider-neutral payload built by openai_to_payload or
            messages_to_payload.

    Returns:
        Every message's text (and the system text, if present), joined by
        blank lines.
    """
    parts: list[str] = []
    if "system" in payload:
        parts.append(payload["system"])
    parts.extend(msg["content"] for msg in payload["messages"])
    return "\n\n".join(parts)
```

Add `estimate_tokens` to the `gatekeep.accounting` import at the top of `gatekeep/app.py`:

```python
from gatekeep.accounting import calculate_cost, estimate_tokens, log_request
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py -v -k "mid_stream or disconnect"`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full `test_endpoint.py` file to check for regressions**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py -v`
Expected: PASS, all tests including `test_streaming_completion`, `test_streaming_completion_logs_request`, `test_streaming_records_ttft_and_duration`, `test_middleware_records_e2e_for_sse_under_the_stream_path`, `test_streaming_records_stream_path` (the clean-stream path must be byte-for-byte unchanged in behavior - spec item 7, "clean stream regression").

- [ ] **Step 6: Commit**

```bash
git add gatekeep/app.py tests/test_endpoint.py
git commit -m "fix(app): _sse records outcome-tagged accounting on every exit path, not just StreamEnd"
```

---

