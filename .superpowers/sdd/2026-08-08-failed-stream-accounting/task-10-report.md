# Task 10 Report: Dashboard success-rate stat (backend)

## What was done

Followed the brief steps in order (TDD: red -> green -> regression check -> lint -> commit).

1. Added two new test functions to `tests/test_dashboard.py` (verbatim from the brief), placed
   immediately after `test_usage_summary_includes_cost_of_failed_rows` and before the
   `# -- usage timeseries --` section marker:
   - `test_usage_summary_includes_failed_count_and_success_rate`
   - `test_usage_summary_success_rate_is_zero_for_an_empty_window`
2. Ran the new tests to confirm they failed with `KeyError: 'failed_count'` (response body did
   not yet contain the new fields).
3. Modified `gatekeep/api/dashboard.py`:
   - Added module-level constant `_FAILED_OUTCOMES = ("provider_error", "client_disconnect")`
     immediately after `_NO_PROMPT_LABEL = "(none)"` (no inline/duplicate definition inside the
     function body).
   - Added `failed_count: int` and `success_rate: float` fields to `UsageSummaryResponse`, after
     `cache_hit_rate`.
   - Added a 9th column to the `totals_row` `select(...)` call: a `func.coalesce(func.sum(case((RequestLog.outcome.in_(_FAILED_OUTCOMES), 1), else_=0)), 0)` expression, appended after the
     existing `cache_hit_count` column.
   - Added `failed_count` as the 9th name in the tuple-unpacking assignment, in exact
     correspondence with the 9th column added to the `select(...)` call.
   - Added `failed_count = int(failed_count)` and computed
     `success_rate = (request_count - failed_count) / request_count if request_count else 0.0`.
   - Added `failed_count=failed_count, success_rate=success_rate,` to the
     `UsageSummaryResponse(...)` construction, after `cache_hit_rate=cache_hit_rate,`.
4. Ran the two new tests again - both passed.
5. Ran the full `tests/test_dashboard.py` file - all 35 tests passed, no regressions to
   `by_model`/`by_key`/`by_prompt` or any other `UsageSummaryResponse` field.
6. Ran `ruff check gatekeep tests` - clean. Ran `ruff format --check gatekeep tests` - flagged
   12 files as needing reformatting, but all except the two files I touched were pre-existing
   formatting drift unrelated to this change (confirmed via `ruff format --check` before my edits
   would have shown the same files). Ran `ruff format` scoped to only the two files I modified
   (`gatekeep/api/dashboard.py`, `tests/test_dashboard.py`) to bring them into compliance without
   touching unrelated files; this only rewrapped a couple of lines that had become short enough
   to fit ruff's line-length rule after the edits (no logic changes). Re-ran the full test suite
   and both ruff checks after formatting to confirm nothing broke.
7. Committed with the exact message from the brief's Step 6.

## Test commands and output

### Step 2: verify tests fail (before implementation)

Command:
```
source .venv/bin/activate && pytest tests/test_dashboard.py -v -k "failed_count or success_rate"
```
Result: `2 failed, 33 deselected, 1 warning` - both failures were
`KeyError: 'failed_count'` at the `assert body["failed_count"] == ...` line, as expected (the
field did not exist in the response body yet).

### Step 4: verify tests pass (after implementation)

Command:
```
source .venv/bin/activate && pytest tests/test_dashboard.py -v -k "failed_count or success_rate"
```
Result:
```
tests/test_dashboard.py::test_usage_summary_includes_failed_count_and_success_rate PASSED [ 50%]
tests/test_dashboard.py::test_usage_summary_success_rate_is_zero_for_an_empty_window PASSED [100%]
2 passed, 33 deselected, 1 warning in 4.19s
```

### Step 5: full dashboard regression run

Command:
```
source .venv/bin/activate && pytest tests/test_dashboard.py -v
```
Result: `35 passed, 1 warning in 10.42s` (re-confirmed again after `ruff format`, same result).
All pre-existing tests passed unchanged, including:
- `test_usage_summary_totals_and_breakdowns` (checks `by_model`/`by_key`/`by_prompt` and all
  original totals fields - `request_count`, `total_tokens`, `prompt_tokens`,
  `completion_tokens`, `spend_usd`, `savings_usd`, `cost_usd`, `cache_hit_count`,
  `cache_hit_rate`)
- `test_usage_summary_respects_time_range`
- `test_usage_summary_filters_by_model`
- `test_usage_summary_includes_cost_of_failed_rows`
- all `usage_timeseries`, `evals`, `prompts`, and `latency_*` tests in the same file

## Ruff output

`ruff check gatekeep tests`:
```
All checks passed!
```

`ruff format --check gatekeep/api/dashboard.py tests/test_dashboard.py` (scoped to touched
files, after running `ruff format` on them):
```
2 files already formatted
```

Note: a repo-wide `ruff format --check gatekeep tests` flags 10 other pre-existing files
(`gatekeep/middleware/auth.py`, `gatekeep/providers/google.py`, and several `tests/test_*.py`
files) as needing reformatting. These are unrelated to this task's changes and were left
untouched per the brief's scope (only files touched by this task were reformatted).

## Commit

```
9360d8e676ef61e09fc21912a173a33fd079acc9 feat(dashboard): add failed_count/success_rate to usage summary
 2 files changed, 47 insertions(+)
```

## Verification of column/tuple correspondence

Final `totals_row` `select(...)` columns (in order): `request_count`, `total_tokens`,
`prompt_tokens`, `completion_tokens`, `cost_usd`, `spend_usd`, `savings_usd`, `cache_hit_count`,
`failed_count` (9 columns) - matches the 9-name tuple-unpacking assignment exactly, confirmed by
direct code read after editing and by the passing test assertions (`request_count == 4`,
`failed_count == 2`, `success_rate == 0.5` for the mixed-outcome seed data), which would have
silently mislabeled values had the order been wrong.

`_FAILED_OUTCOMES` placement: module-level constant directly below `_NO_PROMPT_LABEL = "(none)"`,
no inline/duplicate definition inside `usage_summary`.

## Blockers

None. Postgres/Redis test DB was reachable; `pytest` started and ran normally throughout.
