## Task 3: `StreamTimer.finish(succeeded=...)`

**Files:**
- Modify: `gatekeep/observability/latency.py`
- Test: `tests/test_latency.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `StreamTimer.finish(self, *, succeeded: bool = True) -> LatencyTimings`. Task 4's generator restructure calls this as `timer.finish(succeeded=(outcome == "ok"))`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_latency.py`, after `test_stream_timer_with_no_deltas_leaves_ttft_none`:

```python
def test_stream_timer_finish_failed_uses_last_delta_as_duration_reference():
    """A failed stream's duration_ms is time-to-last-token, not
    time-to-failure: the gap between the last delta and the failure moment
    must not inflate duration_ms."""
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-1")
    timer.provider_started()
    timer.delta()
    last_delta_at = timer._last_delta_at
    timings = timer.finish(succeeded=False)
    assert timings.duration_ms == pytest.approx((last_delta_at - 0.0) * 1000)


def test_stream_timer_finish_failed_before_any_token_has_null_duration():
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-2")
    timer.provider_started()
    timings = timer.finish(succeeded=False)
    assert timings.duration_ms is None
    assert timings.ttft_ms is None


def test_stream_timer_finish_failed_still_publishes_provider_ms():
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-3")
    timer.provider_started()
    timer.delta()
    timings = timer.finish(succeeded=False)
    assert timings.provider_ms is not None
    assert state["provider_ms"] == pytest.approx(timings.provider_ms)


def test_stream_timer_finish_failed_does_not_observe_time_to_last_token():
    ttlt_labels = {"model": "m-fail-4"}
    before = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-4")
    timer.provider_started()
    timer.delta()
    timer.finish(succeeded=False)
    after = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    assert after == before


def test_stream_timer_finish_succeeded_default_is_unchanged():
    """succeeded defaults to True so every pre-existing call site
    (positional `timer.finish()`) keeps its current behavior."""
    ttlt_labels = {"model": "m-succeed-default"}
    before = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-succeed-default")
    timer.provider_started()
    timer.delta()
    timings = timer.finish()
    after = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    assert after > before
    assert timings.duration_ms is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_latency.py -v -k "finish_failed or succeeded_default"`
Expected: FAIL - `test_stream_timer_finish_failed_*` fail with `TypeError: finish() got an unexpected keyword argument 'succeeded'`; `test_stream_timer_finish_succeeded_default_is_unchanged` should already pass (it's a regression pin, not new behavior) but will only run once the others stop erroring at collection... actually keyword-argument TypeErrors are per-test, so this one runs and passes independently. Confirm it passes already.

- [ ] **Step 3: Implement `succeeded` in `StreamTimer.finish`**

Replace the existing `finish` method in `gatekeep/observability/latency.py`:

```python
    def finish(self, *, succeeded: bool = True) -> LatencyTimings:
        """Close out the stream, observe the remaining histograms, return timings.

        `succeeded=False` (a mid-stream provider error or client disconnect)
        changes which reference point `duration_ms` uses: the last delta
        actually emitted, not the failure moment, so a hung failed stream
        doesn't inflate `duration_ms` with dead wait time it spent doing
        nothing useful. `duration_ms` is None on a failed stream if no delta
        ever arrived. `time_to_last_token_seconds` is only observed on a
        clean finish, matching the DB-side exclusion of failed rows from
        percentiles (see gatekeep/api/dashboard.py's `_latency_filters`).
        `provider_ms` is measured to the failure moment either way (real
        upstream time spent, including the failed wait) and is always
        published onto `state` so the middleware's overhead calculation
        still runs.

        Args:
            succeeded: Whether the stream reached a clean StreamEnd. Defaults
                to True, preserving the pre-existing behavior for any
                positional `finish()` call.

        Returns:
            A LatencyTimings for log_request, all-None if no start stamp was
            available.
        """
        if self._started_at is None:
            return _NO_TIMINGS

        now = time.perf_counter()
        provider_ms = (
            None
            if self._provider_started_at is None
            else (now - self._provider_started_at) * 1000
        )
        if succeeded:
            duration_ms = (now - self._started_at) * 1000
            time_to_last_token_seconds.labels(model=self._model).observe(
                duration_ms / 1000
            )
        else:
            duration_ms = (
                None
                if self._last_delta_at is None
                else (self._last_delta_at - self._started_at) * 1000
            )
        # self._state is never None here: self._started_at is only set from
        # state.get(...), so a non-None started_at implies a non-None state.
        self._state["provider_ms"] = provider_ms
        if provider_ms is not None:
            provider_duration_seconds.labels(model=self._model).observe(
                provider_ms / 1000
            )
        return LatencyTimings(
            duration_ms=duration_ms, provider_ms=provider_ms, ttft_ms=self.ttft_ms
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_latency.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add gatekeep/observability/latency.py tests/test_latency.py
git commit -m "feat(latency): StreamTimer.finish gains succeeded flag for accurate failed-stream TTLT"
```

---

