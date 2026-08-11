# Task 3 Report: StreamTimer.finish(succeeded=...)

## What Was Done

Implemented the `succeeded` keyword-only parameter in `StreamTimer.finish()` method to handle failed/aborted stream accounting. This allows accurate timing calculation for failed streams where `duration_ms` should be time-to-last-token rather than time-to-failure.

### Files Modified
- `gatekeep/observability/latency.py` - Updated `finish()` method signature and implementation
- `tests/test_latency.py` - Added 5 new test cases for failed stream scenarios

### Implementation Details
The `finish()` method now:
- Accepts `succeeded: bool = True` as a keyword-only parameter
- When `succeeded=True` (default): Uses current time as reference for `duration_ms` and observes `time_to_last_token_seconds`
- When `succeeded=False`: Uses `_last_delta_at` as reference for `duration_ms` (or None if no delta was sent) and skips observing `time_to_last_token_seconds`
- Always publishes `provider_ms` to `state` for middleware overhead calculation

## Test Results

### New Tests Added
1. `test_stream_timer_finish_failed_uses_last_delta_as_duration_reference` - Verifies duration uses last delta, not failure moment
2. `test_stream_timer_finish_failed_before_any_token_has_null_duration` - Verifies duration_ms is None when no deltas sent
3. `test_stream_timer_finish_failed_still_publishes_provider_ms` - Verifies provider_ms is published even on failure
4. `test_stream_timer_finish_failed_does_not_observe_time_to_last_token` - Verifies TTLT histogram skipped on failure
5. `test_stream_timer_finish_succeeded_default_is_unchanged` - Regression test for backward compatibility

### Test Execution

```bash
source .venv/bin/activate && pytest tests/test_latency.py -v
```

Output:
```
============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/briansia/projects/gatekeep
collected 18 items

tests/test_latency.py::test_observe_non_streaming_returns_duration_and_provider_ms PASSED [  5%]
tests/test_latency.py::test_observe_non_streaming_without_started_at_is_a_no_op PASSED [ 11%]
tests/test_latency.py::test_observe_non_streaming_cache_hit_publishes_provider_ms_none PASSED [ 16%]
tests/test_latency.py::test_observe_non_streaming_publishes_provider_ms_for_middleware PASSED [ 22%]
tests/test_latency.py::test_stream_timer_records_ttft_then_inter_token_gaps PASSED [ 27%]
tests/test_latency.py::test_stream_timer_records_time_to_last_token_not_request_duration PASSED [ 33%]
tests/test_latency.py::test_stream_timer_publishes_provider_ms_onto_state_for_the_middleware PASSED [ 38%]
tests/test_latency.py::test_stream_timer_without_started_at_is_a_no_op PASSED [ 44%]
tests/test_latency.py::test_stream_timer_with_no_deltas_leaves_ttft_none PASSED [ 50%]
tests/test_latency.py::test_stream_timer_finish_failed_uses_last_delta_as_duration_reference PASSED [ 55%]
tests/test_latency.py::test_stream_timer_finish_failed_before_any_token_has_null_duration PASSED [ 61%]
tests/test_latency.py::test_stream_timer_finish_failed_still_publishes_provider_ms PASSED [ 66%]
tests/test_latency.py::test_stream_timer_finish_failed_does_not_observe_time_to_last_token PASSED [ 72%]
tests/test_latency.py::test_stream_timer_finish_succeeded_default_is_unchanged PASSED [ 77%]
tests/test_latency.py::test_mark_sets_model_and_path_on_request_state PASSED [ 83%]
tests/test_latency.py::test_mark_provider_ms_none_is_distinct_from_unset PASSED [ 88%]
tests/test_latency.py::test_mean_itl_is_undefined_below_two_tokens PASSED [ 94%]
tests/test_latency.py::test_mean_itl_is_positive_for_normal_completions PASSED [100%]

============================== 18 passed in 2.93s ==============================
```

### Ruff Checks

```bash
source .venv/bin/activate && ruff check gatekeep tests
```

Result: `All checks passed!`

```bash
source .venv/bin/activate && ruff format --check gatekeep/observability/latency.py tests/test_latency.py
```

Result: `2 files already formatted`

## Commit Information

**Commit Hash:** `3fe6a09`

**Commit Message:** `feat(latency): StreamTimer.finish gains succeeded flag for accurate failed-stream TTLT`

## Summary

All 18 tests pass (9 pre-existing + 5 new). The implementation adds the `succeeded` parameter to `StreamTimer.finish()` with proper handling of:
- Failed stream duration calculation (time-to-last-token vs time-to-failure)
- Conditional histogram observation for `time_to_last_token_seconds` (skipped on failure)
- Consistent `provider_ms` publication for all outcomes
- Full backward compatibility (default `succeeded=True`)
