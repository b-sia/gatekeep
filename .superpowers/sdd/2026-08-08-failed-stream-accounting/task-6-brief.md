## Task 6: Restructure `_messages_sse` (Anthropic-shaped streaming) for full-path accounting

**Files:**
- Modify: `gatekeep/app.py`
- Modify: `tests/test_messages_endpoint.py`

**Interfaces:**
- Consumes: same as Task 5 (`estimate_tokens`, `outcome`, `StreamTimer.finish(succeeded=...)`, `_run_shielded`, `_payload_text`).
- Produces: no new public interface, same shape as Task 5 but for the Anthropic-native endpoint.

This mirrors Task 5 exactly, one endpoint over. Doing it as a separate task (rather than folding into Task 5) keeps each task's diff reviewable against its own test file, matching how `test_endpoint.py` and `test_messages_endpoint.py` already split OpenAI-shaped vs. Anthropic-shaped coverage for every other feature in this codebase.

- [ ] **Step 1: Write the failing reproduction tests**

Add `import asyncio` and `import time` to the top of `tests/test_messages_endpoint.py`. Add a mid-stream-failure provider and fixture, mirroring Task 5's:

```python
class MidStreamFailureProvider:
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
```

Add the tests at the end of the file:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_messages_endpoint.py -v -k "mid_stream or disconnect"`
Expected: FAIL (`NoResultFound`, matching Task 5's Step 2)

- [ ] **Step 3: Restructure `_messages_sse` in `gatekeep/app.py`**

Replace the entire `_messages_sse` function body:

```python
async def _messages_sse(
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
    """Stream a /v1/messages completion as Anthropic-style named Server-Sent Events.

    Emits message_start, content_block_start, a content_block_delta per text
    delta, content_block_stop, message_delta (carrying the authoritative
    final usage and stop_reason on a clean finish), then message_stop.
    Accounting runs in a `finally` block so it fires on every exit path -
    see `_sse`'s docstring for the full outcome-tagging rationale (`ok` /
    `provider_error` / `client_disconnect`), which applies identically here.

    Uses its own DB session for the same reason `_sse` does (the
    request-scoped session dependency is already closed by the time this
    generator keeps running), and wraps the accounting write in
    `_run_shielded` for the same cancellation-safety reason.

    `state` is `request.scope["state"]`, passed in because the generator runs
    after the endpoint has returned and can no longer reach the `request`
    object itself. It doubles as the channel back to the middleware:
    `StreamTimer.finish()` writes `provider_ms` onto it so the middleware can
    derive `gateway_overhead_seconds` once the stream closes, on every exit
    path including a failed one. The middleware still records end-to-end for
    this request; what the generator adds is TTFT, inter-token gaps, and
    time-to-last-token.
    """
    message_id = new_message_id()
    timer = StreamTimer(state, model=model)
    yield _anthropic_event(
        "message_start", message_start_event(id=message_id, model=model)
    )
    yield _anthropic_event("content_block_start", content_block_start_event())

    outcome = "ok"
    input_tokens = output_tokens = 0
    accumulated: list[str] = []
    try:
        timer.provider_started()
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                timer.delta()
                accumulated.append(ev.text)
                yield _anthropic_event(
                    "content_block_delta", content_block_delta_event(ev.text)
                )
            elif isinstance(ev, StreamEnd):
                outcome = "ok"
                input_tokens, output_tokens = ev.input_tokens, ev.output_tokens
                yield _anthropic_event("content_block_stop", content_block_stop_event())
                yield _anthropic_event(
                    "message_delta",
                    message_delta_event(
                        stop_reason=reverse_finish_reason(ev.stop_reason),
                        input_tokens=ev.input_tokens,
                        output_tokens=ev.output_tokens,
                    ),
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
        yield _anthropic_event(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
        )
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
                    response_id=message_id,
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
    # Unreachable on the client-disconnect path, same as _sse.
    yield _anthropic_event("message_stop", message_stop_event())
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_messages_endpoint.py -v -k "mid_stream or disconnect"`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full `test_messages_endpoint.py` file to check for regressions**

Run: `source .venv/bin/activate && pytest tests/test_messages_endpoint.py -v`
Expected: PASS, all tests including `test_streaming_message`, `test_streaming_error_emits_anthropic_shaped_error_event` (still emits the same error event body, now also logs a row), `test_messages_streaming_records_ttft`, `test_messages_streaming_records_stream_path`.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/app.py tests/test_messages_endpoint.py
git commit -m "fix(app): _messages_sse records outcome-tagged accounting on every exit path"
```

---

