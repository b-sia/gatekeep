# Final Whole-Branch Review — Fix Report

Branch: `phase-1-gateway-core`
Scope: three related error-shape findings, one root cause (some paths use `openai_error()`, others rely on FastAPI defaults).

## Root cause

FastAPI's default exception handling serializes any raised `HTTPException` as
`{"detail": <exc.detail>}`. `gatekeep/middleware/auth.py::_unauthorized()` already
builds the correct OpenAI shape (`{"error": {...}}`) as `detail`, but with no
app-level exception handler registered, FastAPI wraps it one level too deep on
the wire: `{"detail": {"error": {...}}}`. Pydantic validation errors
(`RequestValidationError`, raised by FastAPI before the endpoint body runs)
similarly fell through to FastAPI's default `{"detail": [...]}` 422 body, never
touching `openai_error()` at all. The SSE in-stream error path was a separate,
hand-rolled f-string that never went through `openai_error()`/`map_anthropic_error()`
so it drifted from the shape (missing `"code"`).

## Evidence of the bug (before fix)

Ran the app in-process against a live request with no `Authorization` header,
before any handler existed:

```
401 status 401
401 body {'detail': {'error': {'message': "Missing API key. Provide 'Authorization: Bearer <key>'.", 'type': 'authentication_error', 'code': None}}}
```

Confirms Finding 1: a client reading `body["error"]["message"]` per the OpenAI
contract would get a `KeyError` — the real payload is nested under `"detail"`.

(The DB-backed 422 case wasn't reproduced standalone here because it required
the pytest DB fixtures; instead it is covered by the new automated test below,
which failed before the fix — see "Test results" section.)

## Changes

### `gatekeep/app.py`

1. Added imports: `json` (module-level, replacing the function-local import in
   the old `_json()` helper), `fastapi.exceptions.HTTPException as FastAPIHTTPException`,
   `fastapi.exceptions.RequestValidationError`, `starlette.requests.Request`.

2. Registered two app-level exception handlers, right after `app = FastAPI(...)`:

   - `_http_exception_handler` for `FastAPIHTTPException`: if `exc.detail` is
     already a dict containing `"error"` (the shape `_unauthorized()` builds),
     return it verbatim as the JSON body at `exc.status_code` — no extra
     nesting. Otherwise (e.g. a plain-string-detail `HTTPException` raised
     elsewhere in the future), wrap `str(exc.detail)` into the standard
     `openai_error(...)` shape as `invalid_request_error`. This fixes
     Finding 1 without touching `gatekeep/middleware/auth.py` at all — the
     existing `_unauthorized()` shape was already correct, it just needed a
     handler to serialize it properly at the top level.

   - `_validation_exception_handler` for `RequestValidationError`: returns
     `openai_error(400, str(exc), "invalid_request_error")`. This fixes
     Finding 2 — pydantic body-validation failures (e.g. missing `messages`)
     now return `400` with the OpenAI error shape instead of FastAPI's default
     `422` with `{"detail": [...]}`.

3. Rewrote the SSE in-stream error branch in `_sse()`: replaced the hand-rolled
   f-string (which omitted `"code"`) with a real dict serialized via
   `json.dumps`, including `"code": "anthropic_error"` to match
   `map_anthropic_error()`'s shape used elsewhere for the same error class.
   This fixes Finding 3.

4. Deleted the now-unused `_json()` helper. Confirmed via search that its only
   caller was the SSE error branch being replaced; `_event()` uses
   `chunk.model_dump_json()` (a Pydantic method, unrelated) and was untouched.

### `tests/test_endpoint.py`

Added two new tests targeting the wire-level response body, not the raised
exception object (the existing `test_auth.py` assertions on
`ei.value.detail["error"]["type"]` inspect the exception before FastAPI's
handler runs, so they could not have caught Finding 1):

- `test_missing_auth_returns_openai_shaped_401`: POSTs with no auth header,
  asserts `r.status_code == 401` and `r.json()["error"]["type"] ==
  "authentication_error"` — i.e. `error` is a **top-level** key of the JSON
  body, not nested under `detail`.
- `test_invalid_body_returns_openai_shaped_400`: POSTs with a valid API key
  but a body missing the required `messages` field, asserts
  `r.status_code == 400` and `r.json()["error"]["type"] ==
  "invalid_request_error"`.

Ran both new tests against the pre-fix `gatekeep/app.py` (via
`git stash push -- gatekeep/app.py`, keeping the new test file) to confirm
they fail without the handlers:

```
FAILED tests/test_endpoint.py::test_missing_auth_returns_openai_shaped_401
    body = r.json()
>   assert body["error"]["type"] == "authentication_error"
    KeyError: 'error'

FAILED tests/test_endpoint.py::test_invalid_body_returns_openai_shaped_400
>   assert r.status_code == 400
    assert 422 == 400
2 failed, 4 passed in 0.62s
```

This confirms the 401 test fails with `KeyError: 'error'` (body was
`{"detail": {...}}`, no top-level `error` key) and the 400 test fails because
the endpoint returned `422` instead of `400` before the fix. Restored
`gatekeep/app.py` via `git stash pop` and confirmed both pass cleanly after
the fix (see full run below).

No existing test asserted the old (buggy) wire shape, so no test assertions
needed to be changed to match the fix — `test_requires_auth` in
`tests/test_endpoint.py` only checked `status_code == 401` (unaffected), and
`test_auth.py`'s exception-object assertions are orthogonal to wire
serialization and remain valid as-is.

## Test results (`pytest -v`, full suite, after fix)

```
============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0
plugins: anyio-4.14.1, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collected 28 items

tests/test_auth.py::test_extract_bearer_prefers_authorization PASSED     [  3%]
tests/test_auth.py::test_require_api_key_accepts_valid PASSED            [  7%]
tests/test_auth.py::test_require_api_key_rejects_missing PASSED          [ 10%]
tests/test_auth.py::test_require_api_key_rejects_unknown PASSED          [ 14%]
tests/test_auth.py::test_require_api_key_rejects_inactive PASSED         [ 17%]
tests/test_auth.py::test_map_anthropic_error_with_status_and_message PASSED [ 21%]
tests/test_auth.py::test_map_anthropic_error_fallback_defaults PASSED    [ 25%]
tests/test_config.py::test_settings_reads_env PASSED                     [ 28%]
tests/test_config.py::test_unknown_model_alias_default PASSED            [ 32%]
tests/test_db.py::test_database_reachable PASSED                         [ 35%]
tests/test_endpoint.py::test_healthz PASSED                              [ 39%]
tests/test_endpoint.py::test_requires_auth PASSED                        [ 42%]
tests/test_endpoint.py::test_missing_auth_returns_openai_shaped_401 PASSED [ 46%]
tests/test_endpoint.py::test_invalid_body_returns_openai_shaped_400 PASSED [ 50%]
tests/test_endpoint.py::test_non_streaming_completion PASSED             [ 53%]
tests/test_endpoint.py::test_streaming_completion PASSED                 [ 57%]
tests/test_models.py::test_generate_and_hash_are_stable PASSED           [ 60%]
tests/test_models.py::test_api_key_persists PASSED                       [ 64%]
tests/test_openai_schemas.py::test_parses_minimal_request PASSED         [ 67%]
tests/test_openai_schemas.py::test_response_serializes_openai_shape PASSED [ 71%]
tests/test_provider.py::test_complete_returns_normalized_result PASSED   [ 75%]
tests/test_provider.py::test_stream_yields_deltas_then_end PASSED        [ 78%]
tests/test_translation.py::test_resolve_model_alias_passthrough_default PASSED [ 82%]
tests/test_translation.py::test_system_message_lifted_and_sampling_dropped PASSED [ 85%]
tests/test_translation.py::test_default_max_tokens_applied PASSED        [ 89%]
tests/test_translation.py::test_no_conversational_message_raises PASSED  [ 92%]
tests/test_translation.py::test_result_to_openai_maps_usage_and_finish_reason PASSED [ 96%]
tests/test_translation.py::test_stream_chunk_helpers PASSED              [100%]

============================== 28 passed in 1.18s ==============================
```

28/28 passed, no warnings, no skips — pristine.

## Files changed

- `/home/briansia/projects/gatekeep/gatekeep/app.py`
- `/home/briansia/projects/gatekeep/tests/test_endpoint.py`

`gatekeep/middleware/auth.py` and `gatekeep/api/errors.py` were left
unmodified per instructions — their shapes were already correct; only the
missing serialization glue in `app.py` needed fixing.

## Self-review

- Handler for `FastAPIHTTPException` correctly distinguishes "already
  OpenAI-shaped dict detail" (pass through) from "plain string / other detail"
  (wrap) — future `HTTPException(status_code=..., detail="some string")`
  call sites elsewhere in the app will still get a sane OpenAI-shaped body
  instead of crashing or nesting.
- Verified `_json()` had exactly one call site before deletion (the SSE error
  branch); `_event()` is unrelated and untouched.
- Verified the new SSE payload is valid JSON via `json.dumps` (previous
  f-string version relied on `_json()` only escaping the message substring,
  not the whole object — structurally it worked but was fragile/inconsistent
  with the rest of the codebase).
- No behavior change to the happy-path (200) responses, `TranslationError`
  handling in `chat_completions`, or `map_anthropic_error` — those already
  used `openai_error()` correctly and were out of scope.
- Confirmed via `docker compose ps` that both `postgres` and `redis` were
  already Up/healthy before running the suite; no environment setup was
  required.
