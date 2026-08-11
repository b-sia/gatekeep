# Task 8 Report: Dashboard latency-percentile exclusion of failed rows

## Summary
Implemented exclusion of failed-outcome rows from latency percentile queries in the dashboard. The `_latency_filters` function now adds an `outcome` condition that allows only NULL or "ok" outcomes in latency calculations, properly excluding failed rows with `provider_error` or `client_disconnect` outcomes while preserving their accurate duration_ms in the database.

## What was done

1. **Extended `_seed_log` test helper** in `tests/test_dashboard.py`:
   - Added `outcome: str | None = None` parameter
   - Updated docstring to document the parameter's behavior
   - Parameter defaults to None (pre-0013 or successful rows)

2. **Added two new failing tests** in `tests/test_dashboard.py`:
   - `test_latency_summary_excludes_failed_outcome_rows`: Verifies that failed outcome rows don't affect latency summary percentiles
   - `test_latency_timeseries_excludes_failed_outcome_rows`: Verifies that failed outcome rows don't affect latency timeseries buckets

3. **Updated `_latency_filters` function** in `gatekeep/api/dashboard.py`:
   - Added `or_` import from sqlalchemy
   - Added new outcome condition: `or_(RequestLog.outcome.is_(None), RequestLog.outcome == "ok")`
   - Updated docstring to explain the outcome filtering logic
   - This change automatically applies to both `latency_summary` and `latency_timeseries` endpoints

## Test Results

### Initial test run (Step 3 - Verify failure):
```
tests/test_dashboard.py::test_latency_summary_excludes_failed_outcome_rows FAILED [ 50%]
tests/test_dashboard.py::test_latency_timeseries_excludes_failed_outcome_rows FAILED [100%]
```
Expected failure: sample_count was 3 (including failed rows) instead of 1 in summary test, and 2 instead of 1 in timeseries test.

### After implementation (Step 5 - Verify pass):
```
tests/test_dashboard.py::test_latency_summary_excludes_failed_outcome_rows PASSED [ 50%]
tests/test_dashboard.py::test_latency_timeseries_excludes_failed_outcome_rows PASSED [100%]
======================== 2 passed in 4.53s ========================
```

### Full dashboard test suite (Step 6 - Check regressions):
```
tests/test_dashboard.py::test_usage_summary_requires_auth PASSED         [  3%]
tests/test_dashboard.py::test_usage_timeseries_requires_auth PASSED      [  6%]
tests/test_dashboard.py::test_evals_requires_auth PASSED                 [  9%]
tests/test_dashboard.py::test_prompts_requires_auth PASSED               [ 12%]
tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns PASSED [ 15%]
tests/test_dashboard.py::test_usage_summary_respects_time_range PASSED   [ 18%]
tests/test_dashboard.py::test_usage_summary_filters_by_model PASSED      [ 21%]
tests/test_dashboard.py::test_usage_timeseries_buckets_by_day PASSED     [ 25%]
tests/test_dashboard.py::test_usage_timeseries_includes_token_and_spend_fields PASSED [ 28%]
tests/test_dashboard.py::test_usage_timeseries_accepts_minute_interval PASSED [ 31%]
tests/test_dashboard.py::test_usage_timeseries_rejects_invalid_interval PASSED [ 34%]
tests/test_dashboard.py::test_usage_timeseries_by_model_requires_auth PASSED [ 37%]
tests/test_dashboard.py::test_usage_timeseries_by_model_groups_by_bucket_and_model PASSED [ 40%]
tests/test_dashboard.py::test_evals_history_returns_runs_newest_first_and_filters_by_prompt PASSED [ 43%]
tests/test_dashboard.py::test_prompts_list_returns_active_version_num PASSED [ 46%]
tests/test_dashboard.py::test_prompt_versions_timeline_ordered_with_active_flag PASSED [ 50%]
tests/test_dashboard.py::test_prompt_versions_timeline_404_for_unknown_prompt PASSED [ 53%]
tests/test_dashboard.py::test_dashboard_unmatched_api_path_returns_404 PASSED [ 56%]
tests/test_dashboard.py::test_dashboard_api_prefix_alone_returns_404 PASSED [ 59%]
tests/test_dashboard.py::test_latency_summary_requires_auth PASSED       [ 62%]
tests/test_dashboard.py::test_latency_summary_percentiles PASSED         [ 65%]
tests/test_dashboard.py::test_latency_overhead_excludes_uncached_row_with_no_provider_ms PASSED [ 68%]
tests/test_dashboard.py::test_latency_summary_excludes_failed_outcome_rows PASSED [ 71%]
tests/test_dashboard.py::test_latency_timeseries_excludes_failed_outcome_rows PASSED [ 75%]
tests/test_dashboard.py::test_latency_summary_breakdowns PASSED          [ 78%]
tests/test_dashboard.py::test_latency_summary_excludes_rows_with_no_path PASSED [ 81%]
tests/test_dashboard.py::test_latency_summary_empty_window_returns_nulls_not_zeros PASSED [ 84%]
tests/test_dashboard.py::test_latency_summary_filters_by_model PASSED    [ 87%]
tests/test_dashboard.py::test_latency_timeseries_requires_auth PASSED    [ 90%]
tests/test_dashboard.py::test_latency_timeseries_buckets_by_day PASSED   [ 93%]
tests/test_dashboard.py::test_latency_timeseries_excludes_rows_with_no_path PASSED [ 96%]
tests/test_dashboard.py::test_latency_timeseries_empty_window_is_not_an_error PASSED [100%]
======================== 32 passed, 1 warning in 9.94s ========================
```

**Result: All 32 tests pass with no regressions.** All existing tests continue to pass because they default `outcome=None`, which satisfies the new condition `or_(RequestLog.outcome.is_(None), RequestLog.outcome == "ok")`.

## Ruff Verification

```
Ruff check: All checks passed!
Ruff format: 2 files reformatted (auto-formatting applied)
Ruff format (verification): 2 files already formatted
```

## Files Modified
- `gatekeep/api/dashboard.py`: Added `or_` import, updated `_latency_filters` function with outcome filtering logic
- `tests/test_dashboard.py`: Extended `_seed_log` helper with `outcome` parameter, added two new test functions

## Commit Information
Commit hash: `544e9f7`
Commit message: `fix(dashboard): exclude failed-outcome rows from latency percentiles`

## Verification
- Both new tests fail before implementation (sample_count includes failed rows)
- Both new tests pass after implementation (sample_count excludes failed rows)
- Full test suite passes: 32/32 tests passing
- No regressions: All existing tests continue to pass
- Code follows project style: Ruff checks and formatting pass
- Docstrings updated with explanatory comments about outcome filtering
