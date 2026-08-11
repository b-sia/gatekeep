# Task 3 Report: Data model, key hashing, and Alembic migrations

## Summary

Implemented Task 3 exactly per the brief: `gatekeep/auth_keys.py` (key generation/hashing),
replaced the placeholder `gatekeep/models.py` with the real `ApiKey` model, added Alembic
scaffolding (`alembic.ini`, `migrations/env.py`, `migrations/versions/0001_api_keys.py`),
and `scripts/create_key.py`. Followed TDD for the model (RED then GREEN). Verified the
Alembic migration applies cleanly against the running Postgres container. All prior +
new tests pass. Committed.

## Environment

- Postgres + Redis were already running via `docker compose up -d postgres redis`
  (`gatekeep-postgres-1` healthy, `gatekeep-redis-1` up) before starting work.
- `.env` already present locally with `DATABASE_URL=postgresql+asyncpg://gatekeep:gatekeep@localhost:5432/gatekeep`.
- Installed `psycopg2-binary` into `.venv` (not added to `pyproject.toml` — brief's
  commit step doesn't list `pyproject.toml`, so left untouched; see Concerns).

## Step-by-step with evidence

### Step 1: `gatekeep/auth_keys.py`

Written verbatim from the brief (sha256 hex hash, `gk-` prefixed token via `secrets.token_urlsafe(32)`).

### Step 2/3: Failing test (RED)

Wrote `tests/test_models.py` verbatim from the brief.

```
$ pytest tests/test_models.py -v
...
ImportError while importing test module '.../tests/test_models.py'.
tests/test_models.py:4: in <module>
    from gatekeep.models import ApiKey
E   ImportError: cannot import name 'ApiKey' from 'gatekeep.models'
1 error in 0.06s
```

Matches the brief's expected failure exactly.

### Step 4/5: Implement `ApiKey` model (GREEN)

Replaced `gatekeep/models.py` with the brief's exact `ApiKey` SQLAlchemy model
(`id`, `name`, `key_hash` unique, `active` default True, `created_at` tz-aware UTC default).

```
$ pytest tests/test_models.py -v
tests/test_models.py::test_generate_and_hash_are_stable PASSED           [ 50%]
tests/test_models.py::test_api_key_persists PASSED                       [100%]
2 passed in 0.10s
```

Full suite (Tasks 1-3):

```
$ pytest -v
tests/test_config.py::test_settings_reads_env PASSED                     [ 20%]
tests/test_config.py::test_unknown_model_alias_default PASSED            [ 40%]
tests/test_db.py::test_database_reachable PASSED                         [ 60%]
tests/test_models.py::test_generate_and_hash_are_stable PASSED           [ 80%]
tests/test_models.py::test_api_key_persists PASSED                       [100%]
5 passed in 0.19s
```

### Steps 6-8: Alembic scaffolding

- `alembic.ini` — verbatim from brief (script_location = migrations, placeholder URL
  overridden at runtime by `env.py`).
- `migrations/env.py` — verbatim from brief; derives sync psycopg2 URL from
  `get_settings().database_url` by stripping `+asyncpg`, registers `gatekeep.models`
  so `Base.metadata` has the table before autogenerate/migrate.
- `migrations/versions/0001_api_keys.py` — verbatim from brief; creates `api_keys`
  table matching the ORM model (server-side defaults for `active`/`created_at`).

Installed `psycopg2-binary` into `.venv`:

```
$ pip install psycopg2-binary
Successfully installed psycopg2-binary-2.9.12
```

### Step 9: Run migration against Postgres

```
$ alembic upgrade head
(no stdout — logger_root level in alembic.ini is WARN, which suppresses Alembic's
 INFO-level "Running upgrade ... " message; this matches the brief's own alembic.ini
 verbatim, so the lack of printed "Running upgrade -> 0001" text is expected given
 that logging config, not a failure)

$ alembic current
0001 (head)

$ alembic downgrade base && alembic upgrade head   # re-verified idempotently, still silent stdout
$ alembic current
0001 (head)
```

Verified directly against Postgres that the table was created with the exact expected
schema:

```
$ docker compose exec -T postgres psql -U gatekeep -d gatekeep -c "\d api_keys"
                                       Table "public.api_keys"
   Column   |           Type           | Collation | Nullable |               Default
------------+--------------------------+-----------+----------+--------------------------------------
 id         | integer                  |           | not null | nextval('api_keys_id_seq'::regclass)
 name       | character varying(255)   |           | not null |
 key_hash   | character varying(64)    |           | not null |
 active     | boolean                  |           | not null | true
 created_at | timestamp with time zone |           | not null | now()
Indexes:
    "api_keys_pkey" PRIMARY KEY, btree (id)
    "api_keys_key_hash_key" UNIQUE CONSTRAINT, btree (key_hash)
```

This matches the ORM model and the migration definition exactly (PK on `id`, unique
constraint on `key_hash`, `not null` everywhere, correct types).

### Step 10: `scripts/create_key.py`

Written verbatim from the brief. Attempted a manual smoke run afterward
(`python scripts/create_key.py "smoke-test"`); it failed with
`ConnectionResetError: [Errno 104] Connection reset by peer` from asyncpg, and shortly
after, `docker compose ps` itself started failing with
`failed to connect to the docker API at unix:///.../docker.sock` — i.e. the local
Docker Desktop daemon dropped mid-session, unrelated to this script. This happened
*after* all required verification (pytest suite, `alembic upgrade head`, and the `\d
api_keys` check) had already succeeded, so it does not affect the required Task 3
acceptance criteria. Re-running the script once Docker is back up should work; it uses
the same `SessionLocal`/`gatekeep.db` engine that `tests/test_db.py::test_database_reachable`
and `tests/test_models.py::test_api_key_persists` already exercised successfully via
the pytest fixture in this same session, before Docker dropped.

### Step 11: Commit

```
$ git add gatekeep/models.py gatekeep/auth_keys.py alembic.ini migrations scripts/create_key.py tests/test_models.py
$ git commit -m "feat: api_keys model, key hashing, and initial migration"
[phase-1-gateway-core 23bee5d] feat: api_keys model, key hashing, and initial migration
 7 files changed, 192 insertions(+), 1 deletion(-)
 create mode 100644 alembic.ini
 create mode 100644 gatekeep/auth_keys.py
 create mode 100644 migrations/env.py
 create mode 100644 migrations/versions/0001_api_keys.py
 create mode 100644 scripts/create_key.py
 create mode 100644 tests/test_models.py
```

## Files changed

- `gatekeep/models.py` (replaced placeholder with real `ApiKey` model)
- `gatekeep/auth_keys.py` (new)
- `alembic.ini` (new)
- `migrations/env.py` (new)
- `migrations/versions/0001_api_keys.py` (new)
- `scripts/create_key.py` (new)
- `tests/test_models.py` (new)

## Self-review

- All interfaces match the required names exactly: `ApiKey` (id, name, key_hash unique,
  active default True, created_at), `generate_key()` / `hash_key()` in `gatekeep.auth_keys`.
- TDD followed: RED confirmed (`ImportError`) before GREEN (2 passed).
- Migration verified against real Postgres, schema matches ORM model column-for-column.
- Did not modify `.env` or commit `.venv/`; both remain gitignored.
- Did not add `psycopg2-binary` to `pyproject.toml` since the brief's Step 11 commit
  list doesn't include it and only calls it a migration-time install; flagging this
  as a concern below in case a later task or CI expects it declared as a dependency.

## Concerns

1. `psycopg2-binary` is installed in the local `.venv` but not declared anywhere in
   `pyproject.toml`. Anyone else setting up the venv (or CI, or the Docker image) will
   need to `pip install psycopg2-binary` separately before `alembic upgrade head` will
   work. Recommend a follow-up to add it as a dev/optional dependency.
2. `alembic upgrade head` produces no stdout given the brief's `alembic.ini`
   (`logger_root level = WARN` suppresses Alembic's INFO "Running upgrade" message).
   The brief's Step 9 says to expect that text in the output; I verified success via
   `alembic current` (`0001 (head)`) and direct Postgres schema inspection instead,
   since that's the substantively equivalent proof. No code change was needed to
   achieve this — it's a discrepancy between the brief's stated expected log line and its
   own specified `alembic.ini` logging config.
3. Late in the session (after all required verification had already passed), the local
   Docker Desktop daemon dropped and a manual optional smoke-test of
   `scripts/create_key.py` failed with a connection reset. This is an environment/Docker
   Desktop issue, not a code defect — the same DB connectivity path was exercised
   successfully by the pytest suite and by `alembic upgrade head` earlier in the same
   session.

## Fix (review round 1)

Addressed Concern 1 above: `psycopg2-binary` was pip-installed ad hoc into `.venv` but
never declared in `pyproject.toml`, so a fresh clone or CI running `pip install -e ".[dev]"`
would not get it and `alembic upgrade head` would fail with
`ModuleNotFoundError: No module named 'psycopg2'`.

### Change

Added `"psycopg2-binary>=2.9"` to the `dev` list under `[project.optional-dependencies]`
in `pyproject.toml`, alongside `pytest`, `pytest-asyncio`, `httpx`.

### Verification

1. Uninstalled the ad hoc install and reinstalled purely via the `dev` extra, to prove
   the pin (not leftover ad hoc state) is what provides the package:

```
$ pip uninstall -y psycopg2-binary
Successfully uninstalled psycopg2-binary-2.9.12

$ pip install -e ".[dev]"
...
Using cached psycopg2_binary-2.9.12-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (4.3 MB)
...
Successfully installed gatekeep-0.1.0 psycopg2-binary-2.9.12

$ pip show psycopg2-binary
Name: psycopg2-binary
Version: 2.9.12
Location: /home/briansia/projects/gatekeep/.venv/lib64/python3.14/site-packages
```

2. Full test suite, still pristine:

```
$ pytest -v
tests/test_config.py::test_settings_reads_env PASSED                     [ 20%]
tests/test_config.py::test_unknown_model_alias_default PASSED            [ 40%]
tests/test_db.py::test_database_reachable PASSED                         [ 60%]
tests/test_models.py::test_generate_and_hash_are_stable PASSED           [ 80%]
tests/test_models.py::test_api_key_persists PASSED                       [100%]
5 passed in 0.20s
```

3. Migration re-verified against the Postgres container (which had been recreated since
   the last session, so the schema was gone and needed re-applying):

```
$ alembic current
(no output — no revision applied, schema absent after container recreation)

$ alembic upgrade head
(succeeded, silent stdout per alembic.ini logging config — see Concern 2 above)

$ alembic current
0001 (head)
```

### Commit

```
$ git add pyproject.toml && git commit -m "fix: add psycopg2-binary to dev dependencies for alembic"
[phase-1-gateway-core da0c1bb] fix: add psycopg2-binary to dev dependencies for alembic
 1 file changed, 1 insertion(+)
```

Concern 1 is now resolved. Concerns 2 and 3 are unchanged/unaffected by this fix.
