# Task 7 Report: Non-streaming provider-error path

## Status: DONE

## What was done

Followed the brief exactly, in order (TDD):

1. **Replaced the existing test** `test_provider_error_does_not_count_whole_span_as_overhead`
   in `tests/test_endpoint.py` (previously at lines 927-966) with
   `test_provider_error_now_publishes_provider_ms_and_counts_overhead`, using the brief's
   verbatim code. The old test asserted the overhead-count did NOT increase on a
   non-streaming provider error; the new test asserts it DOES increase by 1, and additionally
   verifies a `RequestLog` row exists with `outcome="provider_error"`, zero tokens, zero cost,
   non-null `provider_ms`, and `path="provider"`.

2. **Added a new test** `test_non_streaming_provider_error_logs_outcome_and_overhead` to
   `tests/test_messages_endpoint.py` (appended after
   `test_client_disconnect_before_first_token_has_null_duration`), using the brief's verbatim
   code (a local `FailingProvider` test double whose `complete` raises, monkeypatched into
   `app_module._providers["anthropic"]`). This matches the existing no-docstring convention for
   local test-double classes already used elsewhere in that same file (e.g. the `FailingProvider`
   in `test_streaming_error_emits_anthropic_shaped_error_event`), so no docstrings were added to
   it despite the global docstring rule - the task brief's own constraint says docstring style
   should match "this codebase's existing style," and this file's existing local test doubles
   have none.

3. Verified both new tests **fail** before the fix (`NoResultFound` / stale overhead count),
   confirming the DB/Redis test fixtures work and the tests exercise real gaps.

4. **Implemented the fix** in `gatekeep/app.py`, in both `chat_completions` (`/v1/chat/completions`)
   and `messages` (`/v1/messages`): the `except Exception as exc` block around
   `await provider.complete(payload)` now computes `error_provider_ms`, calls
   `observe_non_streaming(request, model=model, path=_PROVIDER_PATH, provider_ms=error_provider_ms)`
   to publish the metric and get timings, then calls `log_request(...)` with `outcome="provider_error"`,
   `prompt_tokens=0`, `completion_tokens=0`, and the appropriate ID generator
   (`new_completion_id()` for chat_completions, `new_message_id()` for messages) before returning
   the mapped error response. Used the brief's code verbatim.

5. Verified both new tests **pass** after the fix.

6. Ran the full `tests/test_endpoint.py` and `tests/test_messages_endpoint.py` suites - all 51
   tests pass, no regressions.

7. Ran `ruff check` and `ruff format --check` on the touched files - clean. (A `ruff format --check
   gatekeep tests` full-repo run flags 12 pre-existing files unrelated to this task's changes;
   none of the three files this task touched are among them.)

8. Committed with the exact message from the brief's Step 7.

## Test commands and output

### Step 2: verify new tests fail (pre-fix)

```
source .venv/bin/activate && pytest tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead -v
```

Result: 2 failed (as expected)
- `test_provider_error_now_publishes_provider_ms_and_counts_overhead` - FAILED
  (overhead count assertion failed: `before_overhead_count + 1` not reached)
- `test_non_streaming_provider_error_logs_outcome_and_overhead` - FAILED
  (`sqlalchemy.exc.NoResultFound: No row was found when one was required`)

### Step 5: verify new tests pass (post-fix)

```
source .venv/bin/activate && pytest tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead -v
```

Result:
```
tests/test_endpoint.py::test_provider_error_now_publishes_provider_ms_and_counts_overhead PASSED [ 50%]
tests/test_messages_endpoint.py::test_non_streaming_provider_error_logs_outcome_and_overhead PASSED [100%]
========================= 2 passed, 1 warning in 9.76s =========================
```

### Step 6: full endpoint test files, regression check

```
source .venv/bin/activate && pytest tests/test_endpoint.py tests/test_messages_endpoint.py -v
```

Result: **51 passed, 1 warning (pre-existing unrelated deprecation warning from google-genai), 0 failed**

All prior tests in both files pass unchanged, including:
- `test_middleware_overhead_is_exact_on_the_non_streaming_provider_path`
- `test_provider_error_mid_stream_logs_failed_row_with_estimated_tokens` (both endpoint files, streaming path - untouched)
- `test_client_disconnect_mid_stream_logs_failed_row`
- `test_client_disconnect_before_first_token_has_null_duration`

Confirms no regressions from Tasks 1-6's streaming-path fixes and no regressions elsewhere in
either full file.

## Ruff output

```
source .venv/bin/activate && ruff check gatekeep tests
All checks passed!

ruff format --check gatekeep tests
Would reformat: gatekeep/api/dashboard.py
Would reformat: gatekeep/middleware/auth.py
Would reformat: gatekeep/providers/google.py
Would reformat: tests/test_anthropic_schemas.py
Would reformat: tests/test_anthropic_translation.py
Would reformat: tests/test_curation.py
Would reformat: tests/test_dashboard.py
Would reformat: tests/test_google_provider.py
Would reformat: tests/test_openai_provider.py
Would reformat: tests/test_openai_schemas.py
Would reformat: tests/test_request_samples_wiring.py
Would reformat: tests/test_translation.py
12 files would be reformatted, 63 files already formatted
```

None of `gatekeep/app.py`, `tests/test_endpoint.py`, or `tests/test_messages_endpoint.py` (the
three files this task touched) appear in the reformat list - these are pre-existing formatting
issues in files untouched by this task. Confirmed separately:

```
ruff format --check gatekeep/app.py tests/test_endpoint.py tests/test_messages_endpoint.py
3 files already formatted
```

## Files changed

- `/home/briansia/projects/gatekeep/gatekeep/app.py` - added accounting to the provider-error
  `except` blocks in `chat_completions` and `messages`
- `/home/briansia/projects/gatekeep/tests/test_endpoint.py` - replaced
  `test_provider_error_does_not_count_whole_span_as_overhead` with
  `test_provider_error_now_publishes_provider_ms_and_counts_overhead`
- `/home/briansia/projects/gatekeep/tests/test_messages_endpoint.py` - added
  `test_non_streaming_provider_error_logs_outcome_and_overhead`

## Commit

```
1881a278095cad2fef77bf58772e40dd6ba632ff fix(app): non-streaming provider errors publish provider_ms and log outcome=provider_error
```

3 files changed, 93 insertions(+), 14 deletions(-)

## Concerns

None. No blockers encountered - Postgres and Redis test fixtures connected and worked
throughout.
