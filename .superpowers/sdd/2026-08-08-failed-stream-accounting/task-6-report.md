# Task 6 Report: Restructure `_messages_sse` for full-path accounting

## Summary

Restructured `gatekeep/app.py`'s `_messages_sse` generator (the Anthropic-shaped
`/v1/messages` streaming path) to mirror Task 5's `_sse` restructure: a single
`try`/`except (GeneratorExit, asyncio.CancelledError)`/`except Exception`/`finally`
block that records an outcome-tagged `RequestLog` row (`ok` / `provider_error` /
`client_disconnect`) on every exit path, using `estimate_tokens` for
input/output token estimates when no authoritative `StreamEnd` was reached, and
`_run_shielded` to protect the accounting write from repeated cancellation.
Reused `_payload_text` and `_run_shielded` from `gatekeep/app.py` (added in
Tasks 4-5); did not redefine either. `_sse` itself was not touched (confirmed
via `git diff --stat` - only the `_messages_sse` region changed).

## Correction applied (per instructions)

The brief's Step 3 sample code positioned both the `message_start` and
`content_block_start` yields *before* `try:`. This reproduces the same bug
Task 5's implementer found and fixed for `_sse`: a cancellation landing at
the generator's very first or second `__anext__()` (before `try:` has been
entered) would propagate with no active exception handler, so `finally`
never runs and no row gets written - silently reintroducing the bug this
plan exists to fix, at an earlier point in the stream.

Fix applied: moved `try:` to be the first statement after
`timer = StreamTimer(state, model=model)` and the `outcome` /
`input_tokens` / `output_tokens` / `accumulated` initialization, with both
the `message_start` and `content_block_start` yields as the first two
statements inside `try:`, immediately before `timer.provider_started()`.
No other logic in the brief's sample was changed.

## Additional test added

Per instructions, added `test_client_disconnect_before_first_token_has_null_duration`
to `tests/test_messages_endpoint.py` (not in the brief) - it cancels right
after the very first yield (`message_start` only, before `content_block_start`
or any delta), which is the boundary condition that specifically exercises
correct `try:` placement (a cancellation after 3 `__anext__()` calls, as the
brief's own test does, cannot distinguish correct vs. buggy `try:` placement
since execution is already past both initial yields by then either way).

## Files changed

- `gatekeep/app.py` - `_messages_sse` restructured (98 lines changed: 64
  insertions, 34 deletions within that function's region only).
- `tests/test_messages_endpoint.py` - added `import asyncio`, `import time`;
  `MidStreamFailureProvider` class + `mid_stream_failure_client` fixture;
  three new tests:
  - `test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens`
  - `test_client_disconnect_mid_stream_logs_failed_row`
  - `test_client_disconnect_before_first_token_has_null_duration` (the
    extra test required by the correction above)

## Step 2: verify new tests fail before implementation

Command: `source .venv/bin/activate && pytest tests/test_messages_endpoint.py -v -k "mid_stream or disconnect"`

Result: all 3 new tests FAILED with `sqlalchemy.exc.NoResultFound: No row was
found when one was required` (matching Task 5's Step 2 expectation), 12
deselected.

## Step 4: verify new tests pass after implementation

Command: `source .venv/bin/activate && pytest tests/test_messages_endpoint.py -v -k "mid_stream or disconnect"`

Result:
```
tests/test_messages_endpoint.py::test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens PASSED
tests/test_messages_endpoint.py::test_client_disconnect_mid_stream_logs_failed_row PASSED
tests/test_messages_endpoint.py::test_client_disconnect_before_first_token_has_null_duration PASSED
3 passed, 12 deselected, 1 warning in 4.53s
```

## Step 5: full file regression run

Command: `source .venv/bin/activate && pytest tests/test_messages_endpoint.py -v`

Result: `15 passed, 1 warning in 12.18s`. All 15 tests in the file pass,
including (explicitly confirmed unchanged and passing, isolated run):

Command: `pytest tests/test_messages_endpoint.py -v -k "test_streaming_message or test_streaming_error_emits_anthropic_shaped_error_event or test_messages_streaming_records_ttft or test_messages_streaming_records_stream_path"`

```
tests/test_messages_endpoint.py::test_streaming_message PASSED
tests/test_messages_endpoint.py::test_streaming_error_emits_anthropic_shaped_error_event PASSED
tests/test_messages_endpoint.py::test_messages_streaming_records_ttft PASSED
tests/test_messages_endpoint.py::test_messages_streaming_records_stream_path PASSED
4 passed, 11 deselected, 1 warning in 4.79s
```

## Full test suite

Command: `source .venv/bin/activate && pytest`

Result: `367 passed, 1 warning in 75.69s (0:01:15)`. No regressions anywhere
in the suite (includes `tests/test_endpoint.py` - `_sse`'s own tests -
unaffected, confirming `_sse` was left untouched).

## Ruff

Commands:
```
source .venv/bin/activate && ruff check gatekeep tests
source .venv/bin/activate && ruff format --check gatekeep tests
```

`ruff check gatekeep tests`: `All checks passed!` (no lint findings anywhere).

`ruff format --check gatekeep tests`: reported 13 files needing reformat,
all pre-existing and unrelated to this task's changes except
`tests/test_messages_endpoint.py` (which I touched). Ran
`ruff format tests/test_messages_endpoint.py`, which reformatted two spots:
one pre-existing line-wrap on `test_messages_non_streaming_records_provider_path`
(unrelated to my edits, but the file as a whole now needed to be clean) and
one line-wrap in my new `test_client_disconnect_before_first_token_has_null_duration`.
Did not touch the other 12 files (`gatekeep/api/dashboard.py`,
`gatekeep/middleware/auth.py`, `gatekeep/providers/google.py`, and various
other test files) - they are outside this task's scope and were already
non-conformant before this change.

Re-verified after formatting:
```
ruff check gatekeep/app.py tests/test_messages_endpoint.py       -> All checks passed!
ruff format --check gatekeep/app.py tests/test_messages_endpoint.py -> 2 files already formatted
pytest tests/test_messages_endpoint.py -v -> 15 passed
```

## Commit

```
git add gatekeep/app.py tests/test_messages_endpoint.py
git commit -m "fix(app): _messages_sse records outcome-tagged accounting on every exit path"
```

Commit hash: `73b7b19`

## Regression confirmation (explicit, as requested)

- `test_streaming_message`: PASSED, unchanged.
- `test_streaming_error_emits_anthropic_shaped_error_event`: PASSED,
  unchanged (still emits the same `event: error` body; now also logs a
  `provider_error` row, verified separately by the new mid-stream test).
- `test_messages_streaming_records_ttft`: PASSED, unchanged.
- `test_messages_streaming_records_stream_path`: PASSED, unchanged.

## Blockers

None. Postgres/Redis-backed test DB was reachable; `pytest` ran normally
throughout.
