# Final-Review Fix Wave Report

Branch: `fix/failed-stream-accounting`
Date: 2026-08-08

All three mandatory fixes and all five bonus fixes are applied, tested, and
committed as one commit. No blocking issues were found.

---

## MANDATORY FIX 1: stream ending without `StreamEnd` logged as $0 "ok"

### Changes

- `gatekeep/app.py` `_sse`: added `stream_ended = False` before the `try:`,
  set `stream_ended = True` in the `StreamEnd` branch, and added a
  `if not stream_ended: raise RuntimeError(...)` immediately after the
  `async for` loop (still inside `try:`), so the existing
  `except Exception as exc:` handler applies the identical failure-accounting
  logic with zero duplication.
- `gatekeep/app.py` `_messages_sse`: same transformation.
- Both docstrings updated to document this fourth exit path. `_sse` got a new
  explicit bullet alongside the existing three; `_messages_sse`'s existing
  cross-reference sentence to `_sse`'s docstring was extended to name the case
  (that read better than duplicating the bullet, given that function's style of
  deferring to `_sse` for the rationale).

### Tests added

- `tests/test_endpoint.py`: `StreamEndsWithoutMarkerProvider` stub,
  `stream_ends_without_marker_client` fixture, and
  `test_stream_ending_without_streamend_marker_logs_failed_row`.
- `tests/test_messages_endpoint.py`: the same stub and fixture (scoped to the
  `anthropic`/`ollama` providers that file's fixtures patch), plus the
  `/v1/messages` equivalent test asserting `"event: error"` in the body.

### TDD evidence

Command:

```
source .venv/bin/activate && pytest tests/test_endpoint.py tests/test_messages_endpoint.py -q -k "stream_ending_without_streamend_marker"
```

BEFORE the code change (RED - confirms the tests exercise the real bug):

```
=========================== short test summary info ============================
FAILED tests/test_endpoint.py::test_stream_ending_without_streamend_marker_logs_failed_row
FAILED tests/test_messages_endpoint.py::test_stream_ending_without_streamend_marker_logs_failed_row
2 failed, 51 deselected, 1 warning in 4.27s
```

The failure mode confirms the bug precisely - the stream completed cleanly with
no error event at all. Anthropic-side assertion output:

```
>       assert "event: error" in body
E       assert 'event: error' in 'event: message_start\ndata: {"type": "message_start", ...
        "delta": {"type": "text_delta", "text": "ng"}}\n\nevent: message_stop\ndata: {"type": "message_stop"}\n\n'
```

AFTER the code change (GREEN):

```
2 passed, 51 deselected, 1 warning in 4.23s
```

---

## MANDATORY FIX 2: no coverage of the `GeneratorExit`/`aclose()` disconnect path

No production code changed - the existing
`except (GeneratorExit, asyncio.CancelledError):` already handles both. This is
pure confirmation coverage of the delivery MECHANISM (Starlette's `aclose()`)
rather than the exception TYPE.

### Tests added

- `tests/test_endpoint.py`: `test_client_disconnect_via_aclose_logs_failed_row`
  (drives `app_module._sse`, `import time as time_module` local-import style to
  match the neighbouring test).
- `tests/test_messages_endpoint.py`: the same test against
  `app_module._messages_sse`, consuming `message_start` +
  `content_block_start` + first `content_block_delta` before `aclose()`, using
  the module-level `time.perf_counter()` per that file's style.

### TDD evidence

Command:

```
source .venv/bin/activate && pytest tests/test_endpoint.py tests/test_messages_endpoint.py -q -k "disconnect_via_aclose"
```

BEFORE any production change, and unchanged AFTER (these were expected to pass
immediately - this is confirmation coverage, not a bug fix):

```
..                                                                       [100%]
2 passed, 53 deselected, 1 warning in 4.20s
```

Both passed on the first run. `await gen.aclose()` returned normally in both
generators (the generator catches and re-raises `GeneratorExit`, the
successful-close case per the async generator protocol), and the
`client_disconnect` row with `completion_tokens == 1` was written in both
cases. **No hidden bug was uncovered here** - nothing to report.

---

## MANDATORY FIX 3: duplicated non-streaming provider-error accounting

### Changes

- Added `_finish_failed_request` to `gatekeep/app.py`, immediately after
  `_finish_request`, with the full Google-style docstring specified by the
  reviewer (including the rationale for deliberately NOT calling
  `observe_request`).
- Replaced the ~20-line duplicated block in `chat_completions` with a call
  passing `new_completion_id()`.
- Replaced the equivalent block in `messages` with a call passing
  `new_message_id()`, still returning `map_provider_error_anthropic(exc)`.

Net: 44 lines of duplication removed, replaced by one shared helper, following
the file's own `_finish_request` precedent for the success path.

### TDD evidence (pure refactor - existing tests must pass unchanged)

Command:

```
source .venv/bin/activate && pytest tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead -q
```

BEFORE the refactor (baseline):

```
2 passed, 1 warning in 9.44s
```

AFTER the refactor (behavior preserved, tests unchanged):

```
2 passed, 1 warning in 9.49s
```

---

## BONUS FIXES

1. **`_run_shielded` type annotations.** Signature is now
   `async def _run_shielded(coro: Coroutine[Any, Any, Any]) -> Any:`.

   *Deviation, deliberate:* the reviewer specified
   `from typing import Any, Coroutine`. I imported `Coroutine` from
   `collections.abc` instead (`from collections.abc import Coroutine` +
   `from typing import Any`), because that is this codebase's own established
   convention - `gatekeep/db.py`, all four `gatekeep/providers/*.py`, and
   `gatekeep/evals.py` all import ABCs (`AsyncIterator`, `Awaitable`,
   `Callable`) from `collections.abc` and only `Any` from `typing`.
   `typing.Coroutine` has been a deprecated alias since Python 3.9 and this
   project runs 3.14. The annotation itself is exactly as specified. This is
   the same "follow the file's own established convention" principle that
   motivated Fix 3.

2. **`_run_shielded` docstring scope.** Added a paragraph noting it is scoped to
   `finally`-block callers that either already have an exception in flight or
   are about to end the generator, and is not a general-purpose "run to
   completion no matter what" utility, since it silently discards the caller's
   own cancellation once the wrapped coroutine completes.

3. **`outcome == "ok"` assertions on the clean-stream tests.** Added
   `assert log.outcome == "ok"` to `test_streaming_records_stream_path`
   (`tests/test_endpoint.py`) and `test_messages_streaming_records_stream_path`
   (`tests/test_messages_endpoint.py`), each directly after the existing
   `assert log.path == "stream"`.

   Both confirmed passing in the full run below:

   ```
   tests/test_endpoint.py::test_streaming_records_stream_path PASSED        [ 56%]
   tests/test_messages_endpoint.py::test_messages_streaming_records_stream_path PASSED [ 89%]
   ```

   These assertions are meaningful guards against Fix 1 over-reaching: had the
   new `RuntimeError` fired on a clean stream, these two would have caught it.

4. **`StatRow.tsx` comment.** Updated to
   `(requests, cost, tokens, savings, cache hit rate, success rate)`. The
   `lg:grid-cols-6` layout and all responsive breakpoints were left untouched,
   as instructed.

5. (Bonus items 1 and 2 above both target `_run_shielded`; all five listed
   bonus fixes are applied.)

### Explicitly NOT touched, per instructions

The `accumulated: list[str]` memory-profile optimization, the `estimate_tokens`
call-site deduplication, budget/overhead assertions on the Anthropic-side
failure tests, and the 10 unrelated pre-existing `ruff format` failures.

---

## Target test files (both, verbose)

```
source .venv/bin/activate && pytest tests/test_endpoint.py tests/test_messages_endpoint.py -v
```

```
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0
collected 55 items
...
tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead PASSED [ 50%]
tests/test_endpoint.py::test_streaming_records_stream_path PASSED        [ 56%]
tests/test_endpoint.py::test_stream_ending_without_streamend_marker_logs_failed_row PASSED [ 61%]
tests/test_endpoint.py::test_client_disconnect_mid_stream_logs_failed_row PASSED [ 63%]
tests/test_endpoint.py::test_client_disconnect_via_aclose_logs_failed_row PASSED [ 65%]
tests/test_endpoint.py::test_client_disconnect_before_first_token_has_null_duration PASSED [ 67%]
tests/test_messages_endpoint.py::test_messages_streaming_records_stream_path PASSED [ 89%]
tests/test_messages_endpoint.py::test_stream_ending_without_streamend_marker_logs_failed_row PASSED [ 92%]
tests/test_messages_endpoint.py::test_client_disconnect_via_aclose_logs_failed_row PASSED [ 96%]
tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead PASSED [100%]

======================== 55 passed, 1 warning in 20.03s ========================
```

All 55 pass, including the 4 new tests and both pre-existing tests named in
Fix 3.

## Full suite

```
source .venv/bin/activate && pytest tests/ -q
```

```
........................................................................ [ 19%]
........................................................................ [ 38%]
........................................................................ [ 57%]
........................................................................ [ 76%]
........................................................................ [ 95%]
.................                                                        [100%]

377 passed, 1 warning in 77.71s (0:01:17)
```

No regressions anywhere. The single warning is a pre-existing third-party
`DeprecationWarning` from `google/genai/types.py`, unrelated to this change.

## Frontend build

```
cd dashboard && npm run build
```

```
> gatekeep-dashboard@0.1.0 build
> tsc && vite build

vite v5.4.21 building for production...
transforming...
✓ 846 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.42 kB │ gzip:   0.27 kB
dist/assets/index-Li9ujqXt.css    9.77 kB │ gzip:   2.59 kB
dist/assets/index-Chy5l5zc.js   572.71 kB │ gzip: 161.42 kB

(!) Some chunks are larger than 500 kB after minification. ...
✓ built in 1.69s
```

`tsc` clean, build succeeded. The chunk-size notice is pre-existing and
unrelated.

## Ruff

```
ruff check gatekeep tests
```

```
All checks passed!
```

```
ruff format --check gatekeep tests
```

```
Would reformat: gatekeep/middleware/auth.py
Would reformat: gatekeep/providers/google.py
Would reformat: tests/test_anthropic_schemas.py
Would reformat: tests/test_anthropic_translation.py
Would reformat: tests/test_curation.py
Would reformat: tests/test_google_provider.py
Would reformat: tests/test_openai_provider.py
Would reformat: tests/test_openai_schemas.py
Would reformat: tests/test_request_samples_wiring.py
Would reformat: tests/test_translation.py
10 files would be reformatted, 65 files already formatted
```

These are exactly the 10 unrelated pre-existing failures declared out of scope.
None is a file this fix touched. Confirmed against the touched files only:

```
ruff format --check gatekeep/app.py tests/test_endpoint.py tests/test_messages_endpoint.py
```

```
3 files already formatted
```

## Files changed

- `gatekeep/app.py`
- `tests/test_endpoint.py`
- `tests/test_messages_endpoint.py`
- `dashboard/src/components/StatRow.tsx`

## Commit

Single commit, message exactly as specified, no co-author trailer:

```
95e4479f38eb0f5e0d644e4b8ee178f84ddb27d3
```

Verified: no co-author trailer and no agent/AI name in the commit message.
