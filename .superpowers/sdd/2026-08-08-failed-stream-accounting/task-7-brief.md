## Task 7: Non-streaming provider-error path (`/v1/chat/completions` and `/v1/messages`)

**Files:**
- Modify: `gatekeep/app.py`
- Modify: `tests/test_endpoint.py` (update one existing test whose asserted behavior this fix intentionally changes)
- Modify: `tests/test_messages_endpoint.py`

**Interfaces:**
- Consumes: `outcome` param on `log_request` (Task 2).
- Produces: no new interface - fixes the milder non-streaming half of issue #17.

**Important:** `tests/test_endpoint.py::test_provider_error_does_not_count_whole_span_as_overhead` currently asserts that `gateway_overhead_seconds`'s count does **not** increase on a non-streaming provider error, because today `provider_ms` is never published on that path. This fix is exactly what removes that limitation - the count **will** increase by 1 after this change. Update that test as part of this task rather than leaving it contradicting the new behavior.

- [ ] **Step 1: Write the failing tests**

In `tests/test_endpoint.py`, replace `test_provider_error_does_not_count_whole_span_as_overhead` (the whole function) with:

```python
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
    before_duration_count = sample_for(
        metrics.request_duration_seconds, "_count", labels
    )
    before_overhead_count = sample_for(
        metrics.gateway_overhead_seconds, "_count", labels
    )

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
        sample_for(metrics.request_duration_seconds, "_count", labels)
        == before_duration_count + 1
    )
    assert (
        sample_for(metrics.gateway_overhead_seconds, "_count", labels)
        == before_overhead_count + 1
    ), "provider_ms is now published even on failure, so overhead must be observed"

    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.outcome == "provider_error"
    assert log.prompt_tokens == 0
    assert log.completion_tokens == 0
    assert log.cost_usd == 0
    assert log.provider_ms is not None
    assert log.path == "provider"
```

Run: `grep -n "test_provider_error_does_not_count_whole_span_as_overhead" tests/test_endpoint.py` first to get its exact current line range before replacing, since it's a large function.

In `tests/test_messages_endpoint.py`, add (there is no existing non-streaming-provider-error test here to replace):

```python
async def test_non_streaming_provider_error_logs_outcome_and_overhead(
    client, raw_key, session, monkeypatch
):
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead -v`
Expected: FAIL - the overhead-count assertion fails (still equals `before_overhead_count`), and/or `NoResultFound` on the log query (no row written today).

- [ ] **Step 3: Fix the provider-error branch in `chat_completions`**

In `gatekeep/app.py`, inside `chat_completions`, replace:

```python
    # Marked before the call so a provider error still carries labels.
    mark(request, path=_PROVIDER_PATH)
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        return map_provider_error(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
```

with:

```python
    # Marked before the call so a provider error still carries labels.
    mark(request, path=_PROVIDER_PATH)
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        error_provider_ms = (time.perf_counter() - provider_started) * 1000
        error_timings = observe_non_streaming(
            request, model=model, path=_PROVIDER_PATH, provider_ms=error_provider_ms
        )
        await log_request(
            session,
            key_id=key_id,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            response_id=new_completion_id(),
            prompt_name=req.prompt_name,
            routed_from=routed_from,
            prompt_version_num=served_prompt_version,
            path=_PROVIDER_PATH,
            outcome="provider_error",
            duration_ms=error_timings.duration_ms,
            provider_ms=error_timings.provider_ms,
            ttft_ms=error_timings.ttft_ms,
        )
        return map_provider_error(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
```

- [ ] **Step 4: Fix the equivalent branch in `messages`**

In `gatekeep/app.py`, inside `messages`, replace:

```python
    # Marked before the call so a provider error still carries labels.
    mark(request, path=_PROVIDER_PATH)
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        return map_provider_error_anthropic(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
```

with:

```python
    # Marked before the call so a provider error still carries labels.
    mark(request, path=_PROVIDER_PATH)
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        error_provider_ms = (time.perf_counter() - provider_started) * 1000
        error_timings = observe_non_streaming(
            request, model=model, path=_PROVIDER_PATH, provider_ms=error_provider_ms
        )
        await log_request(
            session,
            key_id=key_id,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            response_id=new_message_id(),
            prompt_name=req.prompt_name,
            routed_from=routed_from,
            prompt_version_num=served_prompt_version,
            path=_PROVIDER_PATH,
            outcome="provider_error",
            duration_ms=error_timings.duration_ms,
            provider_ms=error_timings.provider_ms,
            ttft_ms=error_timings.ttft_ms,
        )
        return map_provider_error_anthropic(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead -v`
Expected: PASS

- [ ] **Step 6: Run both full endpoint test files to check for regressions**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py tests/test_messages_endpoint.py -v`
Expected: PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add gatekeep/app.py tests/test_endpoint.py tests/test_messages_endpoint.py
git commit -m "fix(app): non-streaming provider errors publish provider_ms and log outcome=provider_error"
```

---

