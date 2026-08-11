# Task 9: Cost-inclusion regression check for failed rows - Report

## Summary
Successfully added a regression-pinning test to verify that cost aggregation is unaffected by the failed-row outcome tagging introduced in earlier tasks of this plan.

## What Was Done
1. Added `test_usage_summary_includes_cost_of_failed_rows()` to `tests/test_dashboard.py`
   - Located after the existing usage summary tests (line 230)
   - Tests that both successful and failed request log rows are included in cost aggregation
   - Verifies `request_count=2`, `cost_usd=0.75`, and `spend_usd=0.75` from two rows costing $0.50 and $0.25
   - Uses existing `_key_id()` and `_seed_log()` helpers, passing `outcome="ok"` and `outcome="provider_error"`

2. Applied ruff formatting to the modified file
   - Ruff check: All checks passed
   - Ruff format: File reformatted to match project standards

3. Committed changes to git branch `fix/failed-stream-accounting`

## Test Execution Results

### Initial Test Run
```
pytest tests/test_dashboard.py -v -k includes_cost_of_failed_rows

============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- /home/briansia/projects/gatekeep/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/briansia/projects/gatekeep
configfile: pyproject.toml
plugins: anyio-4.14.1, asyncio-4.14.1, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 33 items / 32 deselected / 1 selected

tests/test_dashboard.py::test_usage_summary_includes_cost_of_failed_rows PASSED [100%]

=============================== warnings summary ===============================
.venv/lib64/python3.14/site-packages/google/genai/types.py:42
  /home/briansia/projects/google/genai/types.py:42: DeprecationWarning: '_UnionGenericAlias' is deprecated and slated for removal in Python 3.17
    VersionedUnionType = Union[builtin_types.UnionType, _UnionGenericAlias]

-- Docs: https://docs.pytest.org/en/latest/deprecations.html
======================== 1 passed, 32 deselected in 4.12s ========================
```

### Final Test Run (After Formatting)
```
pytest tests/test_dashboard.py -v -k includes_cost_of_failed_rows

============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- /home/briansia/projects/gatekeep/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/briansia/projects/gatekeep
configfile: pyproject.toml
plugins: anyio-4.14.1, asyncio-1.4.0, pluggy-1.6.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 33 items / 32 deselected / 1 selected

tests/test_dashboard.py::test_usage_summary_includes_cost_of_failed_rows PASSED [100%]

=============================== warnings summary ===============================
.venv/lib64/python3.14/site-packages/google/genai/types.py:42
  /home/briansia/projects/google/genai/types.py:42: DeprecationWarning: '_UnionGenericAlias' is deprecated and slated for removal in Python 3.17
    VersionedUnionType = Union[builtin_types.UnionType, _UnionGenericAlias]

-- Docs: https://docs.pytest.org/en/latest/deprecations.html
======================== 1 passed, 32 deselected in 4.17s ========================
```

## Ruff Check Results

### Linting
```
ruff check gatekeep tests
All checks passed!
```

### Formatting
```
ruff format --check tests/test_dashboard.py
1 file already formatted
```

## Git Commit

**Commit hash:** `01fd6e5`

**Commit message:**
```
test(dashboard): pin cost aggregates as unaffected by failed-row outcome tagging
```

**Files modified:** `tests/test_dashboard.py` (1 file changed, 25 insertions)

## Verification Summary

- [x] Test passes immediately (no production code changes required)
- [x] Cost aggregation correctly includes failed rows in total cost and spend
- [x] Ruff linter and formatter checks pass
- [x] Commit created with correct message
- [x] Test is regression-pinning type (verifies existing correct behavior)

This task successfully confirms that the design specification requirement - "failed rows' estimated cost still counts (the money was spent)" - is maintained throughout the implementation of failed-stream accounting in earlier tasks.
