## Task 2: `RequestLog.outcome` column + migration

**Files:**
- Modify: `gatekeep/models.py`
- Create: `migrations/versions/0013_request_log_outcome.py`
- Modify: `gatekeep/accounting.py` (wire `outcome` through `log_request`)
- Test: `tests/test_accounting.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `RequestLog.outcome: str | None`, and `log_request(..., outcome: str = "ok")` persists it. Task 4 (generator restructure) and Task 6 (non-streaming provider-error path) both call `log_request(..., outcome=...)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_accounting.py`:

```python
import pytest_asyncio
from sqlalchemy import select

from gatekeep.accounting import log_request
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import ApiKey, RequestLog


@pytest_asyncio.fixture
async def key_id(session):
    raw = generate_key()
    key = ApiKey(name="accounting-test", key_hash=hash_key(raw))
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key.id


async def test_log_request_defaults_outcome_to_ok(session, key_id):
    log = await log_request(
        session,
        key_id=key_id,
        model="claude-sonnet-5",
        prompt_tokens=1,
        completion_tokens=1,
        response_id="resp-outcome-default",
    )
    assert log.outcome == "ok"


async def test_log_request_persists_explicit_outcome(session, key_id):
    log = await log_request(
        session,
        key_id=key_id,
        model="claude-sonnet-5",
        prompt_tokens=1,
        completion_tokens=1,
        response_id="resp-outcome-explicit",
        outcome="provider_error",
    )
    await session.refresh(log)
    fetched = (
        await session.execute(
            select(RequestLog).where(RequestLog.id == log.id)
        )
    ).scalar_one()
    assert fetched.outcome == "provider_error"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_accounting.py -v -k outcome`
Expected: FAIL with `TypeError: log_request() got an unexpected keyword argument 'outcome'`

- [ ] **Step 3: Add the column to `gatekeep/models.py`**

In `gatekeep/models.py`, inside `class RequestLog`, immediately after the `path` column (after the line `path: Mapped[str | None] = mapped_column(String(32), nullable=True)`, before `__table_args__`):

```python
    # Which of "ok", "provider_error", "client_disconnect" this request
    # ended as. NULL on any row written before this column existed (or by a
    # caller that doesn't pass it) - treated as "ok"-equivalent everywhere
    # this is read (dashboard.py's _latency_filters, the success-rate
    # aggregate), since failed rows were never logged at all before #17.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

- [ ] **Step 4: Create the migration**

Create `migrations/versions/0013_request_log_outcome.py`:

```python
"""add request_logs.outcome

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable `outcome` column.

    Nullable so pre-migration rows stay NULL. Unlike `path` (migration
    0012), no index accompanies this column and no `autocommit_block` is
    needed - a plain `ADD COLUMN ... NULL` is a fast metadata-only change
    on Postgres, not a full table rewrite.
    """
    op.add_column("request_logs", sa.Column("outcome", sa.String(32), nullable=True))


def downgrade() -> None:
    """Drop the `outcome` column."""
    op.drop_column("request_logs", "outcome")
```

- [ ] **Step 5: Wire `outcome` through `log_request`**

In `gatekeep/accounting.py`, update `log_request`'s signature and body. Add `outcome: str = "ok",` to the keyword-only parameters (place it near `path`), update the docstring, and add `outcome=outcome` to the `RequestLog(...)` constructor call:

```python
async def log_request(
    session: AsyncSession,
    *,
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    response_id: str,
    cached: bool = False,
    cache_key: str | None = None,
    cost_usd_override: float | None = None,
    prompt_name: str | None = None,
    routed_from: str | None = None,
    prompt_version_num: int | None = None,
    duration_ms: float | None = None,
    provider_ms: float | None = None,
    ttft_ms: float | None = None,
    path: str | None = None,
    outcome: str = "ok",
) -> RequestLog:
```

Add to the docstring, after the existing `path` paragraph:

```
    `outcome` is one of "ok" (default), "provider_error", or
    "client_disconnect", recording how the request ended. A mid-stream
    provider failure or client disconnect still gets a row (see #17) with
    estimated tokens/cost instead of no row at all; `outcome` is what lets
    any consumer (the dashboard latency queries, a success-rate stat)
    distinguish those estimated rows from authoritative clean ones.
```

Add `outcome=outcome,` to the `RequestLog(...)` construction, alongside the existing `path=path,` line.

- [ ] **Step 6: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_accounting.py -v`
Expected: PASS (6 tests)

- [ ] **Step 7: Run the full existing test suite to check for regressions**

Run: `source .venv/bin/activate && pytest tests/ -x -q`
Expected: PASS, no regressions (the new column is nullable and defaults `outcome="ok"` in `log_request`, so no existing caller or assertion should break).

- [ ] **Step 8: Commit**

```bash
git add gatekeep/models.py gatekeep/accounting.py migrations/versions/0013_request_log_outcome.py tests/test_accounting.py
git commit -m "feat(accounting): add request_logs.outcome column and thread it through log_request"
```

---

