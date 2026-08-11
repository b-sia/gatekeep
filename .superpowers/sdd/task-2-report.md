# Task 2 Report: Docker Compose stack + async DB engine

## Summary

Implemented exactly per brief: `Dockerfile`, `docker-compose.yml` (postgres/pgvector,
redis, gateway services), `gatekeep/db.py` (async engine/session), `tests/conftest.py`
(shared autouse schema fixture + `session`/`db_ping` fixtures), `tests/test_db.py`,
and the `gatekeep/models.py` placeholder. One additional change beyond the brief's
file list was required to make the test suite pass reliably (see "Deviation" below).

## Steps followed (brief order)

1. Wrote `Dockerfile` verbatim.
2. Wrote `docker-compose.yml` verbatim.
3. Wrote `gatekeep/db.py` verbatim (`Base`, `engine`, `SessionLocal`, `get_session`).
4. Wrote `tests/conftest.py` verbatim.
5. Wrote `tests/test_db.py` verbatim.
6. Brought up Postgres/Redis and ran the RED test.
7. Created placeholder `gatekeep/models.py`.
8. Reran the test — GREEN.
9. Committed.

## TDD evidence

### Setup

```
$ cp -n .env.example .env
$ docker compose up -d postgres redis
...
Container gatekeep-postgres-1 Started
Container gatekeep-redis-1 Started
$ docker compose ps postgres --format '{{.Health}}'
healthy
```

### RED (Step 6) — before `gatekeep/models.py` existed

```
$ pytest tests/test_db.py -v
...
    @pytest_asyncio.fixture(autouse=True)
    async def _create_schema():
        # Import models so their tables register on Base.metadata.
>       import gatekeep.models  # noqa: F401
        ^^^^^^^^^^^^^^^^^^^^^^
E       ModuleNotFoundError: No module named 'gatekeep.models'

tests/conftest.py:10: ModuleNotFoundError
=========================== short test summary info ============================
ERROR tests/test_db.py::test_database_reachable - ModuleNotFoundError: No mod...
=============================== 1 error in 0.05s ===============================
```

Fails for the expected reason exactly as the brief predicted.

### GREEN (Step 8) — after placeholder `gatekeep/models.py` added

```
$ pytest tests/test_db.py -v
tests/test_db.py::test_database_reachable PASSED                         [100%]
============================== 1 passed in 0.03s ===============================
```

### Full suite (Task 1 + Task 2 tests together)

First run, with only the brief's files in place (no pyproject.toml change yet):

```
$ pytest -v
tests/test_config.py::test_settings_reads_env PASSED
tests/test_config.py::test_unknown_model_alias_default PASSED
tests/test_db.py::test_database_reachable ERROR
...
E   RuntimeError: Task <Task pending name='Task-16' ...> got Future <Future pending ...>
    attached to a different loop
========================== 2 passed, 1 error in 0.21s ==========================
```

This reproduced deterministically on repeated runs. Root cause (see "Deviation" below).

After the fix, full suite run three times in a row, all green:

```
$ pytest -v
tests/test_config.py::test_settings_reads_env PASSED                     [ 33%]
tests/test_config.py::test_unknown_model_alias_default PASSED            [ 66%]
tests/test_db.py::test_database_reachable PASSED                         [100%]
============================== 3 passed in 0.03s ===============================
```
(repeated twice more with identical "3 passed" result)

`docker compose config -q` also validated cleanly (no YAML/schema errors).

## Deviation from brief's file list: `pyproject.toml`

**Problem found:** `tests/conftest.py`'s `_create_schema` fixture is `autouse=True`,
so it also runs for Task 1's *synchronous* tests in `tests/test_config.py`, not just
async DB tests. With `pytest-asyncio`'s default (function-scoped) event loop, each
test function — sync or async — that triggers this async fixture gets a fresh event
loop for fixture setup/teardown. The module-level `engine` in `gatekeep/db.py` is a
single `AsyncEngine` with a connection pool shared across the whole test session; once
an asyncpg connection is checked into that pool bound to one event loop, reusing it
from a *different* event loop (the next test's fresh loop) raises
`RuntimeError: ... attached to a different loop`. This is a known asyncpg/pytest-asyncio
interaction, not a bug in the brief's `db.py`/`conftest.py` code itself — it's exposed by
running the *combined* suite (Task 1 sync tests + Task 2 async tests) with the default
pytest-asyncio loop scope.

**Fix applied:** Added two ini options to `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

This pins all async fixtures and async tests to one shared event loop for the whole
pytest session, so the engine's pooled connections are always used from the same loop
they were created in. Verified this resolves the failure (see full-suite runs above)
and does not affect the two Task 1 sync tests (they don't use event loops directly).

This file wasn't in the brief's "Files" list, but the brief's own `conftest.py` is
explicitly designed to be shared by "later tasks too," and the parent task instructions
required a full green `pytest -v` run before committing. Without this change, any task
that adds both sync and async tests (or multiple async test modules) run in the same
`pytest` invocation would intermittently/deterministically fail with this same
cross-loop error. I included this file in the commit alongside the brief's listed files.

## Files changed / created

- `Dockerfile` (new)
- `docker-compose.yml` (new)
- `gatekeep/db.py` (new)
- `gatekeep/models.py` (new, placeholder)
- `tests/conftest.py` (new)
- `tests/test_db.py` (new)
- `pyproject.toml` (modified — added the two asyncio loop-scope ini options; see Deviation above)

## Self-review

- All brief file contents (Dockerfile, docker-compose.yml, gatekeep/db.py,
  tests/conftest.py, tests/test_db.py, gatekeep/models.py) were copied verbatim from
  the brief — no naming/interface deviations. `gatekeep.db.Base`, `gatekeep.db.engine`,
  `gatekeep.db.SessionLocal`, `gatekeep.db.get_session()` all present exactly as
  specified for downstream tasks to depend on.
- `.env` was created from `.env.example` via `cp -n` and is git-ignored (confirmed via
  `git status` — not shown as untracked/staged).
- `.venv/` untouched by git (pre-existing, git-ignored).
- Postgres brought up via `docker-compose.yml`'s `pgvector/pgvector:pg16` image and
  confirmed healthy via the compose healthcheck before running tests.
- `docker compose config -q` validates the compose file syntactically; did not attempt
  a full `docker compose build`/`up gateway` (Dockerfile references `gatekeep/app.py`
  and `migrations/`/`alembic.ini`, none of which exist yet — expected, since those land
  in later tasks; the `gateway` service is not exercised by this task's tests).
- Ran full test suite 3 times consecutively post-fix; consistently 3 passed, 0 flaky.

## Concerns

- The `pyproject.toml` change is a deviation from the brief's literal file list, done
  to satisfy the "full test suite must pass" requirement from the parent task. Flagging
  for reviewer awareness in case a different fix (e.g., `NullPool` on the test engine,
  or a scoped/rebuilt engine per test module) is preferred by the project's testing
  conventions going forward. The chosen fix is minimal (2 ini lines) and has no
  functional impact on non-test code.
- The `gateway` service in `docker-compose.yml` will fail to build until
  `gatekeep/app.py`, `migrations/`, and `alembic.ini` exist (future tasks) — this is
  expected/by design per the brief and not something this task's test suite exercises.

## Fix (review round 1)

A reviewer raised two Important findings on the original fix above (the session-scoped
event loop and the `pytest-asyncio>=0.23` floor). Both addressed without touching
`gatekeep/db.py`.

### Finding 1 — `pytest-asyncio>=0.23` floor too low

`asyncio_default_test_loop_scope` (used in the round-1 fix) was only added in
pytest-asyncio 0.24; `>=0.23` could resolve to a version that silently ignores it.

**Change:** `pyproject.toml` `[project.optional-dependencies] dev`:
```diff
-    "pytest-asyncio>=0.23",
+    "pytest-asyncio>=0.24",
```

### Finding 2 — session-scoped loop collapses per-test isolation suite-wide

The round-1 fix (`asyncio_default_fixture_loop_scope = "session"` and
`asyncio_default_test_loop_scope = "session"`) worked around the asyncpg
cross-loop error, but at the cost of putting *every* async test in the suite on one
shared event loop for the whole session — removing per-test isolation project-wide,
not just for the DB tests that needed it.

**Change 1 — `pyproject.toml` `[tool.pytest.ini_options]`:** keep
`asyncio_mode = "auto"`; set the fixture loop scope explicitly to `"function"`
(silences the pytest-asyncio unset-config warning while restoring per-test isolation);
remove `asyncio_default_test_loop_scope` entirely (unknown ini key on pytest-asyncio
>=0.24 in auto mode — leaving it in would cause an unknown-ini-key warning).

```diff
 asyncio_mode = "auto"
-asyncio_default_fixture_loop_scope = "session"
-asyncio_default_test_loop_scope = "session"
+asyncio_default_fixture_loop_scope = "function"
```

**Change 2 — `tests/conftest.py`:** added `await engine.dispose()` as the last
statement of the autouse `_create_schema` fixture's teardown, after the final
`drop_all`. This runs under the *current* test's event loop (before that loop is torn
down) and discards all pooled asyncpg connections, so the next test — running on its
own fresh function-scoped loop — always opens brand-new connections instead of reusing
ones bound to a stale loop. This targets the actual cross-loop reuse bug directly
instead of forcing every test onto one shared loop.

```diff
     yield
     async with engine.begin() as conn:
         await conn.run_sync(Base.metadata.drop_all)
+    await engine.dispose()
```

`gatekeep/db.py` was not touched — its pooled engine remains the production config.

### Commands run

```
docker compose up -d postgres redis
. .venv/bin/activate
pip install -e ".[dev]"
pip show pytest-asyncio   # -> Version: 1.4.0 (>= 0.24 floor satisfied)
pytest -v                 # run 1
pytest -v                 # run 2
```

### Run 1 output (pristine, no warnings)

```
============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- /home/briansia/projects/gatekeep/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/briansia/projects/gatekeep
configfile: pyproject.toml
plugins: anyio-4.14.1, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 3 items

tests/test_config.py::test_settings_reads_env PASSED                     [ 33%]
tests/test_config.py::test_unknown_model_alias_default PASSED            [ 66%]
tests/test_db.py::test_database_reachable PASSED                         [100%]

============================== 3 passed in 0.06s ===============================
```

### Run 2 output (pristine, no warnings — identical result)

```
============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-9.1.1, pluggy-1.6.0 -- /home/briansia/projects/gatekeep/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/briansia/projects/gatekeep
configfile: pyproject.toml
plugins: anyio-4.14.1, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 3 items

tests/test_config.py::test_settings_reads_env PASSED                     [ 33%]
tests/test_config.py::test_unknown_model_alias_default PASSED            [ 66%]
tests/test_db.py::test_database_reachable PASSED                         [100%]

============================== 3 passed in 0.06s ===============================
```

Both runs: 3 passed, 0 warnings, 0 errors. No pytest-asyncio deprecation warning and no
unknown-ini-key warning (confirmed via `asyncio_default_test_loop_scope=function`
appearing in the plugin banner only as pytest-asyncio's own derived default reporting,
not as a set ini key — the ini file itself no longer sets it).

### Files changed

- `pyproject.toml` (modified — `pytest-asyncio>=0.24` floor; loop-scope ini fix)
- `tests/conftest.py` (modified — added `await engine.dispose()` to fixture teardown)
- `gatekeep/db.py` — unchanged, as required.
