# Task 5 Report: Restructure `_sse` for full-path accounting

## What was done

1. **Step 1 (failing tests):** Added `MidStreamFailureProvider` next to `BrokenProvider`, the
   `mid_stream_failure_client` fixture next to `broken_client`, and the four tests from the brief
   (`test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens`,
   `test_provider_error_mid_stream_observes_gateway_overhead`,
   `test_client_disconnect_mid_stream_logs_failed_row`,
   `test_client_disconnect_before_first_token_has_null_duration`) to `tests/test_endpoint.py`, all
   copied verbatim from the brief.

2. **Step 2 (verify failing):** Confirmed all 4 new tests failed with `NoResultFound` (no
   `RequestLog` row written), reproducing issue #17 as expected.

3. **Step 3 (implementation):**
   - Added `estimate_tokens` to the `gatekeep.accounting` import in `gatekeep/app.py`.
   - Added the `_payload_text` helper as its own top-level function, placed after `_run_shielded`
     and before `_sse`, verbatim from the brief.
   - Replaced the entire `_sse` function body with the brief's restructured version (try/except/
     finally shape, `outcome` tracking, `_run_shielded`-wrapped accounting in `finally`).
   - `_messages_sse` and everything else in `gatekeep/app.py` were left untouched.

   **One necessary deviation from the brief's Step 3 code, required to make the brief's own
   Step 1 tests (and the authoritative design spec) pass:** the brief's verbatim code placed the
   initial `yield _event(role_chunk(...))` *before* the `try:` block. I verified with an isolated
   Python repro that when `asyncio.CancelledError` is thrown into a generator while it is
   suspended at a `yield` that is lexically outside a `try`, the exception propagates immediately
   and the `try/except/finally` that appears later in the function body never executes at all -
   it does not get a chance to run. This directly contradicts:
   - The design spec's own item 3 (`docs/superpowers/specs/2026-08-07-failed-stream-accounting-design.md:237`):
     "Failure before first token. `duration_ms`/`ttft_ms` are `NULL`; row still written with the
     right `outcome`."
   - The brief's own test `test_client_disconnect_before_first_token_has_null_duration`, which
     drives the generator through exactly one `__anext__()` (the role chunk only, no delta) and
     then throws `CancelledError`, expecting a logged row.

   I moved the `try:` (and the `outcome`/`input_tokens`/`accumulated` initialization) to wrap the
   role-chunk yield as well, so that a cancellation at that very first suspension point is now
   caught and accounted for:

   ```python
   completion_id = new_completion_id()
   created = int(time.time())
   timer = StreamTimer(state, model=model)

   outcome = "ok"
   input_tokens = output_tokens = 0
   accumulated: list[str] = []
   try:
       yield _event(role_chunk(id=completion_id, created=created, model=model))
       timer.provider_started()
       async for ev in provider.stream(payload):
           ...
   ```

   No other logic was changed - the except/finally bodies are verbatim from the brief. This is a
   pure control-flow-scope correction, not a reinterpretation of the accounting logic itself.

4. **Step 4 (verify new tests pass):** All 4 tests pass after the fix (see command/output below).

5. **Step 5 (full-file regression check):** All 35 tests in `tests/test_endpoint.py` pass,
   including the 5 clean-stream regression tests named in the brief.

6. **Step 6 (commit):** Committed with the exact message from the brief.

## Test commands and output

### Step 4: new tests only

```
$ source .venv/bin/activate && pytest tests/test_endpoint.py -v -k "mid_stream or disconnect"
```

```
collected 35 items / 31 deselected / 4 selected

tests/test_endpoint.py::test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens PASSED [ 25%]
tests/test_endpoint.py::test_provider_error_mid_stream_observes_gateway_overhead PASSED [ 50%]
tests/test_endpoint.py::test_client_disconnect_mid_stream_logs_failed_row PASSED [ 75%]
tests/test_endpoint.py::test_client_disconnect_before_first_token_has_null_duration PASSED [100%]

================= 4 passed, 31 deselected, 1 warning in 4.85s ==================
```

(Before the try-block fix described above, this same command produced `3 passed, 1 failed` -
`test_client_disconnect_before_first_token_has_null_duration` failed with `NoResultFound`.)

### Step 5: full file regression

```
$ source .venv/bin/activate && pytest tests/test_endpoint.py -v
```

```
collected 35 items

tests/test_endpoint.py::test_run_shielded_completes_the_coroutine_despite_repeated_cancellation PASSED
tests/test_endpoint.py::test_run_shielded_returns_the_coroutines_result_when_not_cancelled PASSED
tests/test_endpoint.py::test_healthz PASSED
tests/test_endpoint.py::test_requires_auth PASSED
tests/test_endpoint.py::test_missing_auth_returns_openai_shaped_401 PASSED
tests/test_endpoint.py::test_invalid_body_returns_openai_shaped_400 PASSED
tests/test_endpoint.py::test_non_streaming_completion PASSED
tests/test_endpoint.py::test_openai_prefixed_model_routes_to_openai_provider_response PASSED
tests/test_endpoint.py::test_streaming_completion PASSED
tests/test_endpoint.py::test_non_streaming_completion_logs_request PASSED
tests/test_endpoint.py::test_streaming_completion_logs_request PASSED
tests/test_endpoint.py::test_prompt_name_substitutes_active_template_as_system_message PASSED
tests/test_endpoint.py::test_candidate_at_100_pct_always_serves_candidate_template PASSED
tests/test_endpoint.py::test_candidate_at_0_pct_never_serves_candidate_template PASSED
tests/test_endpoint.py::test_candidate_split_routes_a_mix_of_active_and_candidate_requests PASSED
tests/test_endpoint.py::test_promote_prompt_unaffected_by_inflight_candidate_via_endpoint PASSED
tests/test_endpoint.py::test_rate_limit_exhaustion_returns_429_with_retry_after PASSED
tests/test_endpoint.py::test_budget_cap_allows_below_cap_then_rejects_once_exceeded PASSED
tests/test_endpoint.py::test_unknown_prompt_name_returns_openai_shaped_400 PASSED
tests/test_endpoint.py::test_route_by_cost_with_prompt_name_substitutes_cheaper_qualifying_model PASSED
tests/test_endpoint.py::test_route_by_cost_without_prompt_name_is_a_noop PASSED
tests/test_endpoint.py::test_route_by_cost_defaults_to_false_and_never_substitutes PASSED
tests/test_endpoint.py::test_non_streaming_records_latency_columns PASSED
tests/test_endpoint.py::test_streaming_records_ttft_and_duration PASSED
tests/test_endpoint.py::test_cache_hit_leaves_provider_ms_null PASSED
tests/test_endpoint.py::test_middleware_records_e2e_for_sse_under_the_stream_path PASSED
tests/test_endpoint.py::test_middleware_overhead_is_exact_on_the_non_streaming_provider_path PASSED
tests/test_endpoint.py::test_provider_error_does_not_count_whole_span_as_overhead PASSED
tests/test_endpoint.py::test_non_streaming_records_path_matching_the_metric_label PASSED
tests/test_endpoint.py::test_cache_hit_records_cache_exact_path PASSED
tests/test_endpoint.py::test_streaming_records_stream_path PASSED
tests/test_endpoint.py::test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens PASSED
tests/test_endpoint.py::test_provider_error_mid_stream_observes_gateway_overhead PASSED
tests/test_endpoint.py::test_client_disconnect_mid_stream_logs_failed_row PASSED
tests/test_endpoint.py::test_client_disconnect_before_first_token_has_null_duration PASSED

======================== 35 passed, 1 warning in 17.05s ========================
```

Confirmed explicitly: `test_streaming_completion`, `test_streaming_completion_logs_request`,
`test_streaming_records_ttft_and_duration`,
`test_middleware_records_e2e_for_sse_under_the_stream_path`, and
`test_streaming_records_stream_path` all pass unchanged - the clean-stream regression check.

## Ruff

```
$ source .venv/bin/activate && ruff check gatekeep tests
All checks passed!

$ ruff format --check gatekeep/app.py tests/test_endpoint.py
2 files already formatted
```

`ruff format --check gatekeep tests` (whole tree) reports 13 pre-existing files elsewhere in the
repo that would be reformatted (`gatekeep/api/dashboard.py`, `gatekeep/middleware/auth.py`,
`gatekeep/providers/google.py`, and several other test files) - none of these were touched by this
task, so left as-is per the brief's "fix any findings in files you touched" instruction.

## Commit

```
e2773407c8b22bda77f84a25dc9e1019f4942397 fix(app): _sse records outcome-tagged accounting on every exit path, not just StreamEnd
```

2 files changed, 269 insertions(+), 34 deletions(-) (`gatekeep/app.py`, `tests/test_endpoint.py`).

## Concerns / notes for follow-up

- The brief's Step 3 code (as written in `task-5-brief.md`) has the initial role-chunk `yield`
  positioned outside the `try` block. This makes `test_client_disconnect_before_first_token_has_null_duration`
  fail, and contradicts design-spec item 3. I corrected it by moving `try:` to also wrap that
  yield (see "What was done" above) - a pure scope change, no logic changed. **Task 6, which
  applies the same restructure to `_messages_sse`, should apply the equivalent fix** (the opening
  `message_start`/`content_block_start` event(s) for that generator must also be inside the
  `try`), or it will hit the identical bug.
