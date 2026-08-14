# Multi-tenancy via Accounts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce an `accounts` (tenant) layer so every stored artifact and every dashboard/budget/cache read is scoped to the account derived server-side from the authenticated API key, making keys disposable credentials rather than the identity itself.

**Architecture:** A new `accounts` table becomes the tenancy root; `ApiKey` gains a non-null `account_id` FK. Every content/usage table (`request_logs`, `request_samples`, `cached_responses`, `eval_cases`) gains a denormalized `account_id` written at capture time so provenance survives key rotation. Budget and rate-limit pooling, the response caches, and the dashboard all switch from per-key to per-account scoping. `account_id` is always derived from the authenticated key, never accepted from the client.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Postgres + pgvector, Redis, pytest-asyncio, ruff.

## Global Constraints

- **Source spec:** `docs/superpowers/specs/2026-08-12-multi-tenancy-accounts-design.md`. Every decision number below (e.g. "decision 4") refers to that document.
- **`account_id` is never client-supplied.** It is always derived server-side from the authenticated `ApiKey` (`ApiKey.account_id`). No endpoint may accept it as a query/body parameter.
- **Tests build the schema from the ORM models** via `Base.metadata.create_all` (`tests/conftest.py`), *not* from Alembic migrations. The end-state model is therefore the source of truth for tests; migrations are maintained by convention (matching existing `migrations/versions/0001`-`0013`) and are not exercised by the suite. Every task updates **both** the model and a matching migration.
- **No em dashes** anywhere (code, comments, docstrings, commit messages). Use a plain `-`.
- **Docstrings required** on every new function/method/class: purpose, parameters, returns, exceptions where applicable (repo convention, see existing modules).
- **ruff** is the linter+formatter (`line-length = 100`, config in `pyproject.toml`); a pre-commit hook runs `ruff` on commit. Run `ruff check .` and `ruff format .` before each commit; keep lines <= 100 chars.
- **Commit messages:** conventional prefix (`feat:`, `refactor:`, `test:`, etc.). Do NOT add any agent co-author trailer.
- **Test DB:** requires `TEST_DATABASE_URL` (distinct from `DATABASE_URL`) and a running Postgres with the `vector` extension, plus Redis. Run the suite with `pytest`. Dev-environment note: this repo has historically needed a `DOCKER_HOST` override and care with venv/`PYTHONPATH` in worktrees.
- **Rate-limit-config interpretation (decision 5):** the "tables affected" list says `accounts` holds "rate-limit config", but decision 5's body only mandates *pooling* (the account is the unit that is rate-limited) and explicitly defers per-key/per-account sub-limits as YAGNI. This plan therefore re-keys the rate-limit token bucket by `account_id` while keeping the capacity/refill numbers in global `Settings` (today's behavior, reproduced exactly). No per-account rate-limit numeric columns are added. If a reviewer wants per-account rate overrides now, that is a scoped addition to Task 5, not a rework.

## File Structure

New files:
- `migrations/versions/0014_accounts.py` - accounts table, `api_keys.account_id`, name-uniqueness swap, budget move onto accounts (Task 1).
- `migrations/versions/0015_request_logs_account_id.py` - `request_logs.account_id` (Task 2).
- `migrations/versions/0016_request_samples_account_id.py` - `request_samples.account_id` (Task 3).
- `migrations/versions/0017_cached_responses_account_id.py` - `cached_responses.account_id` + unique swap (Task 4).
- `migrations/versions/0018_budget_ratelimit_account.py` - drop `api_keys.monthly_budget_usd` (Task 5).
- `migrations/versions/0019_eval_cases_account_id.py` - `eval_cases.account_id` (Task 6).

Modified files (by responsibility):
- `gatekeep/models.py` - the `Account` model + `account_id` columns on five tables.
- `gatekeep/middleware/auth.py` - unchanged return type; `account_id` rides on the returned `ApiKey`.
- `gatekeep/middleware/budget.py`, `gatekeep/middleware/ratelimit.py`, `gatekeep/accounting.py` - per-account pooling.
- `gatekeep/middleware/cache_exact.py`, `gatekeep/middleware/cache_semantic.py`, `gatekeep/samples.py`, `gatekeep/curation.py` - `account_id` write/read + cache partitioning.
- `gatekeep/app.py` - thread `account_id` from the authenticated key into every write path (including the streaming generators).
- `gatekeep/api/dashboard.py` - account-scoped reads + `is_operator` gating.
- `gatekeep/cli.py` - `set-budget` targets an account.
- `tests/helpers.py` - shared account/key factory; `tests/*` - attach an account to every `ApiKey(...)`.

## Table end-state (`account_id` nullability)

| Table | `account_id` | Backfill source | Notes |
|---|---|---|---|
| `api_keys` | NOT NULL | one new account per key | `name` unique per `(account_id, name)`; per-key `monthly_budget_usd` dropped |
| `request_logs` | NOT NULL | `key_id -> api_keys.account_id` | written in `log_request` |
| `request_samples` | NOT NULL | `key_id -> api_keys.account_id` | written in `record_request_sample` |
| `cached_responses` | NOT NULL | existing rows **deleted** (no owner derivable; cache is disposable/TTL'd) | unique `(account_id, exact_hash)` |
| `eval_cases` | NULLABLE | left NULL | manual cases legitimately have no account; only curated cases carry one |
| `accounts` | - | new table | holds `monthly_budget_usd`, `is_operator` |

---

### Task 1: Accounts table, `ApiKey.account_id`, name-uniqueness, budget-onto-account

Foundational. Adds the `accounts` table and a **non-null** `ApiKey.account_id`, swaps `ApiKey.name` uniqueness to `(account_id, name)`, and copies each key's budget onto its account (enforcement still reads the key column; the switch happens in Task 5). Because `account_id` becomes non-null, every `ApiKey(...)` construction in the tests must attach an account - a shared helper is added for that.

Implements: core tenancy layer, decision 7 (name uniqueness), decision 8 (one account per key, nullable -> backfill -> NOT NULL), and the `accounts`-side of decision 5 (budget column present on accounts; is_operator present for decision 6).

**Files:**
- Modify: `gatekeep/models.py` (add `Account`; add `ApiKey.account_id`; drop `ApiKey`'s global `name` unique + add composite `UniqueConstraint`; keep `ApiKey.monthly_budget_usd` for now)
- Create: `migrations/versions/0014_accounts.py`
- Modify: `tests/helpers.py` (account/key factory)
- Modify: every test file that constructs `ApiKey(...)` (see step 6)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `gatekeep.models.Account` with fields `id: int`, `name: str`, `monthly_budget_usd: float | None`, `is_operator: bool`, `created_at: datetime`; `ApiKey.account_id: int` (non-null FK to `accounts.id`).
- Produces: `tests/helpers.py::create_account(session, *, name="acct", monthly_budget_usd=None, is_operator=False) -> Account` and `create_key(session, account, *, name="c", key_hash="h", monthly_budget_usd=None, active=True) -> ApiKey`.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing model test**

Add to `tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from gatekeep.models import Account, ApiKey


@pytest.mark.asyncio
async def test_account_owns_keys_and_name_unique_per_account(session):
    acct_a = Account(name="team-a")
    acct_b = Account(name="team-b")
    session.add_all([acct_a, acct_b])
    await session.flush()

    # Same key name is allowed under two different accounts.
    session.add(ApiKey(name="prod", key_hash="h1", account_id=acct_a.id))
    session.add(ApiKey(name="prod", key_hash="h2", account_id=acct_b.id))
    await session.commit()

    # Duplicate name within one account is rejected.
    session.add(ApiKey(name="prod", key_hash="h3", account_id=acct_a.id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_account_defaults(session):
    acct = Account(name="team-c")
    session.add(acct)
    await session.commit()
    assert acct.is_operator is False
    assert acct.monthly_budget_usd is None
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_models.py::test_account_owns_keys_and_name_unique_per_account tests/test_models.py::test_account_defaults -v`
Expected: FAIL with `ImportError`/`AttributeError` on `Account` (does not exist yet).

- [ ] **Step 3: Add the `Account` model and `ApiKey.account_id`**

In `gatekeep/models.py`, add `UniqueConstraint` to the SQLAlchemy imports (`from sqlalchemy import (... UniqueConstraint ...)`), then add the `Account` model above `ApiKey` and modify `ApiKey`:

```python
class Account(Base):
    """A tenant (team) that owns API keys and all data captured through them.

    Accounts are the tenancy root: keys are disposable credentials onto an
    account, and every content/usage row is scoped to the account derived
    server-side from the authenticated key. `monthly_budget_usd` is the
    account's shared monthly spend pool (None means unlimited); `is_operator`
    grants the fleet-wide dashboard view (decision 6). There is deliberately
    no role hierarchy or RBAC - operator status is a single boolean.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Shared monthly USD spend cap for the whole account; None means unlimited.
    # Enforced by gatekeep.middleware.budget against cumulative
    # request_logs.cost_usd for the account in the current UTC calendar month.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_operator: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
```

Then modify `ApiKey`: add the `account_id` column, drop the inline `unique=True` on `name`, and add a composite unique constraint. `monthly_budget_usd` stays (removed in Task 5).

```python
class ApiKey(Base):
    """A client's gateway API key, stored as a salted hash rather than plaintext.

    A key is a disposable credential onto its `Account`: rotating or revoking
    it never orphans history, which hangs off the account. `name` is unique
    only within an account (decision 7), so one tenant's labels never collide
    with another's namespace.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # Deprecated: superseded by Account.monthly_budget_usd. Retained until
    # Task 5 flips enforcement to the account pool, then dropped.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uq_api_keys_account_id_name"),
    )
```

- [ ] **Step 4: Add the test helpers**

Add to `tests/helpers.py`:

```python
from gatekeep.models import Account, ApiKey


async def create_account(
    session,
    *,
    name: str = "acct",
    monthly_budget_usd: float | None = None,
    is_operator: bool = False,
) -> Account:
    """Create and flush an Account for tests, returning it with its id populated.

    Flushes (not commits) so callers can add keys in the same transaction.
    """
    account = Account(
        name=name, monthly_budget_usd=monthly_budget_usd, is_operator=is_operator
    )
    session.add(account)
    await session.flush()
    return account


async def create_key(
    session,
    account: Account,
    *,
    name: str = "c",
    key_hash: str = "h",
    monthly_budget_usd: float | None = None,
    active: bool = True,
) -> ApiKey:
    """Create and flush an ApiKey attached to `account`, returning it with its id."""
    key = ApiKey(
        name=name,
        key_hash=key_hash,
        account_id=account.id,
        monthly_budget_usd=monthly_budget_usd,
        active=active,
    )
    session.add(key)
    await session.flush()
    return key
```

- [ ] **Step 5: Run the new model test to confirm it passes**

Run: `pytest tests/test_models.py::test_account_owns_keys_and_name_unique_per_account tests/test_models.py::test_account_defaults -v`
Expected: PASS.

- [ ] **Step 6: Fix every existing `ApiKey(...)` construction to attach an account**

The 17 files listed by `grep -rln "ApiKey(" tests/` each build an `ApiKey` without `account_id`; they now raise `IntegrityError` (null FK) or `UniqueConstraint`/lookup issues. In **each** test that constructs an `ApiKey`, create an `Account` first and pass `account_id=account.id`. Prefer the helpers from Step 4. Worked example, `tests/test_auth.py`:

```python
# before:
#   session.add(ApiKey(name="c", key_hash=hash_key(raw)))
# after:
from tests.helpers import create_account

account = await create_account(session)
session.add(ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id))
```

Files to update (every `ApiKey(...)` in each): `tests/test_samples.py`, `tests/test_models.py`, `tests/test_cache_semantic.py`, `tests/test_auth.py`, `tests/test_messages_endpoint.py`, `tests/test_cache_exact.py`, `tests/test_budget.py`, `tests/test_cli.py`, `tests/test_curation.py`, `tests/test_metrics.py`, `tests/test_dashboard.py`, `tests/test_accounting.py`, `tests/test_e2e_phase2.py`, `tests/test_endpoint.py`, `tests/test_eval_models.py`, `tests/test_ratelimit.py`, `tests/test_request_samples_wiring.py`. Keep each test's existing intent; only add the account and the `account_id` kwarg. Where a test builds several keys meant to be independent tenants, give each its own account; where they are meant to be the same tenant, share one account.

- [ ] **Step 7: Run the full suite to confirm it is green again**

Run: `pytest -q`
Expected: PASS (all pre-existing tests plus the two new model tests). Fix any remaining `IntegrityError` by attaching an account in the offending test.

- [ ] **Step 8: Write migration 0014**

Create `migrations/versions/0014_accounts.py`. Mirrors decision 8's rollout inside one `upgrade()`: create table, add nullable column, backfill one account per key (copying the key's budget), swap the name-uniqueness constraint, then tighten to NOT NULL.

```python
"""accounts table and api_keys.account_id

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-14

Introduces the tenancy layer (decisions 5, 7, 8): one account per existing
key, account_id backfilled then tightened to NOT NULL, api_keys.name made
unique per (account_id, name), and each key's monthly budget copied onto its
new account (enforcement flips to the account pool in migration 0018).
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("monthly_budget_usd", sa.Float(), nullable=True),
        sa.Column("is_operator", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("api_keys", sa.Column("account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_api_keys_account_id", "api_keys", "accounts", ["account_id"], ["id"]
    )

    # One account per existing key (decision 8). The account inherits the key's
    # name and monthly budget so today's per-key behavior is reproduced exactly.
    op.execute(
        """
        INSERT INTO accounts (name, monthly_budget_usd, is_operator, created_at)
        SELECT name, monthly_budget_usd, false, now() FROM api_keys ORDER BY id
        """
    )
    # Pair each key with the account created from it. Both were inserted in id
    # order, so row_number() over each lines them up one-to-one.
    op.execute(
        """
        WITH k AS (
            SELECT id AS key_id, row_number() OVER (ORDER BY id) AS rn FROM api_keys
        ),
        a AS (
            SELECT id AS account_id, row_number() OVER (ORDER BY id) AS rn FROM accounts
        )
        UPDATE api_keys SET account_id = a.account_id
        FROM k JOIN a ON k.rn = a.rn
        WHERE api_keys.id = k.key_id
        """
    )
    op.alter_column("api_keys", "account_id", nullable=False)

    # Name uniqueness moves from global to per-account (decision 7).
    op.drop_constraint("api_keys_name_key", "api_keys", type_="unique")
    op.create_unique_constraint(
        "uq_api_keys_account_id_name", "api_keys", ["account_id", "name"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_api_keys_account_id_name", "api_keys", type_="unique")
    op.create_unique_constraint("api_keys_name_key", "api_keys", ["name"])
    op.drop_constraint("fk_api_keys_account_id", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "account_id")
    op.drop_table("accounts")
```

Note: confirm the existing global-name unique constraint's name (`api_keys_name_key` is Postgres's default for a column-level `unique=True`). If `\d api_keys` shows a different name, use that in `drop_constraint`.

- [ ] **Step 9: Commit**

```bash
ruff check . && ruff format --check .
git add gatekeep/models.py tests/ migrations/versions/0014_accounts.py
git commit -m "feat: add accounts tenancy layer and api_keys.account_id"
```

---

### Task 2: `request_logs.account_id` (decision 9)

Add a denormalized, non-null `account_id` to `request_logs`, written at capture time in `log_request`, and thread it from the authenticated key through every logging call site in `app.py` (including the streaming generators, which run after the request-scoped session closes and receive scalars, not the `ApiKey` object).

**Files:**
- Modify: `gatekeep/models.py` (`RequestLog.account_id` + index)
- Modify: `gatekeep/accounting.py` (`log_request` gains `account_id`)
- Modify: `gatekeep/app.py` (`_finish_request`, `_finish_failed_request`, `_sse`, `_messages_sse`, and the two endpoints)
- Create: `migrations/versions/0015_request_logs_account_id.py`
- Test: `tests/test_accounting.py`

**Interfaces:**
- Consumes: `Account`, `ApiKey.account_id` (Task 1).
- Produces: `log_request(session, *, key_id, account_id, model, ...)` - `account_id: int` is now a required keyword argument.
- Produces: `RequestLog.account_id: int` (non-null).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_accounting.py`:

```python
from sqlalchemy import select

from gatekeep.accounting import log_request
from gatekeep.models import RequestLog
from tests.helpers import create_account, create_key


@pytest.mark.asyncio
async def test_log_request_stamps_account_id(session):
    account = await create_account(session)
    key = await create_key(session, account, key_hash="acct-log")
    await session.commit()

    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-1",
    )
    row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.account_id == account.id
    assert row.key_id == key.id
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_accounting.py::test_log_request_stamps_account_id -v`
Expected: FAIL with `TypeError` (`log_request` has no `account_id` argument).

- [ ] **Step 3: Add the column and index**

In `gatekeep/models.py`, add to `RequestLog` (after `key_id`):

```python
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
```

and add an index to `RequestLog.__table_args__` so account-scoped dashboard/budget aggregates do not scan:

```python
        # Dashboard and budget aggregates filter by account_id + created_at
        # once scoping lands (decisions 5, 6, 9); this composite serves them.
        Index("ix_request_logs_account_id_created_at", "account_id", "created_at"),
```

- [ ] **Step 4: Thread `account_id` through `log_request`**

In `gatekeep/accounting.py`, add `account_id: int` as a required keyword param on `log_request` (place it right after `key_id`), set it on the `RequestLog(...)`, and document it. The `record_spend` call at the end still uses `key_id` in this task (budget moves in Task 5):

```python
async def log_request(
    session: AsyncSession,
    *,
    key_id: int,
    account_id: int,
    model: str,
    ...
) -> RequestLog:
    """... (existing docstring) ...

    `account_id` is the tenant the request is attributed to, derived
    server-side from the authenticated key. It is denormalized onto the row
    (rather than joined through key_id) so attribution survives key rotation
    or revocation.
    """
    ...
    log = RequestLog(
        key_id=key_id,
        account_id=account_id,
        model=model,
        ...
    )
```

- [ ] **Step 5: Thread `account_id` through the app call sites**

In `gatekeep/app.py`:

1. `_finish_request` and `_finish_failed_request`: add `account_id: int` to their signatures and pass it into their `log_request(...)` calls.
2. The two non-streaming endpoints (`chat_completions`, `messages`): right after `key_id = key.id`, add `account_id = key.account_id` (read up front, same lazy-refresh reasoning as `key_id`), and pass `account_id=account_id` into every `_finish_request` / `_finish_failed_request` call in that endpoint.
3. `_sse` and `_messages_sse`: add `account_id: int` as a keyword param and pass it into their `log_request(...)` call inside the `finally` block's `_record()`.
4. The two `StreamingResponse(_sse(...))` / `_messages_sse(...)` dispatch sites: pass `account_id=account_id`.

Grep to confirm none are missed:

```bash
grep -n "log_request(\|_finish_request(\|_finish_failed_request(\|_sse(\|_messages_sse(" gatekeep/app.py
```

Every call to those four must now pass `account_id`.

- [ ] **Step 6: Run accounting + app tests**

Run: `pytest tests/test_accounting.py tests/test_endpoint.py tests/test_messages_endpoint.py tests/test_metrics.py -q`
Expected: PASS. Any red test is a missed `account_id=` at a call site; fix it.

- [ ] **Step 7: Write migration 0015**

Create `migrations/versions/0015_request_logs_account_id.py`:

```python
"""request_logs.account_id (denormalized, decision 9)

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_logs", sa.Column("account_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE request_logs SET account_id = api_keys.account_id
        FROM api_keys WHERE request_logs.key_id = api_keys.id
        """
    )
    op.alter_column("request_logs", "account_id", nullable=False)
    op.create_foreign_key(
        "fk_request_logs_account_id", "request_logs", "accounts", ["account_id"], ["id"]
    )
    op.create_index(
        "ix_request_logs_account_id_created_at",
        "request_logs",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_logs_account_id_created_at", table_name="request_logs")
    op.drop_constraint("fk_request_logs_account_id", "request_logs", type_="foreignkey")
    op.drop_column("request_logs", "account_id")
```

- [ ] **Step 8: Run the full suite and commit**

Run: `pytest -q` then:

```bash
ruff check . && ruff format --check .
git add gatekeep/models.py gatekeep/accounting.py gatekeep/app.py migrations/versions/0015_request_logs_account_id.py tests/test_accounting.py
git commit -m "feat: denormalize account_id onto request_logs"
```

---

### Task 3: `request_samples.account_id` (decision 4)

Add a non-null denormalized `account_id` to `request_samples`, written in `record_request_sample`, threaded from the authenticated key at the two capture sites in `app.py`. This column is the substrate the eval-case provenance tags in Task 6 read from.

**Files:**
- Modify: `gatekeep/models.py` (`RequestSample.account_id`)
- Modify: `gatekeep/samples.py` (`record_request_sample` gains `account_id`)
- Modify: `gatekeep/app.py` (the two `record_request_sample(...)` call sites)
- Create: `migrations/versions/0016_request_samples_account_id.py`
- Test: `tests/test_samples.py`

**Interfaces:**
- Consumes: `Account` (Task 1), `account_id = key.account_id` already read in both endpoints (Task 2).
- Produces: `record_request_sample(session, *, key_id, account_id, prompt_name, model, input_messages, output_text) -> RequestSample` - `account_id: int` required.
- Produces: `RequestSample.account_id: int` (non-null).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_samples.py`:

```python
@pytest.mark.asyncio
async def test_record_request_sample_stamps_account(session):
    account = await create_account(session)
    key = await create_key(session, account, key_hash="s")
    await session.commit()

    sample = await record_request_sample(
        session,
        key_id=key.id,
        account_id=account.id,
        prompt_name="system-context",
        model="claude-sonnet-5",
        input_messages=[{"role": "user", "content": "hi"}],
        output_text="hello",
    )
    assert sample.account_id == account.id
```

Ensure the test imports `record_request_sample` and the helpers.

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_samples.py::test_record_request_sample_stamps_account -v`
Expected: FAIL with `TypeError` (no `account_id` param).

- [ ] **Step 3: Add the column**

In `gatekeep/models.py`, add to `RequestSample` (after `key_id`):

```python
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
```

- [ ] **Step 4: Thread `account_id` through `record_request_sample`**

In `gatekeep/samples.py`, add `account_id: int` (after `key_id`) to `record_request_sample`, set it on the `RequestSample(...)`, and update the docstring to note it is the tenant derived from the authenticated key, denormalized so per-tenant deletion and eval-case provenance survive key rotation.

- [ ] **Step 5: Update the two app call sites**

In `gatekeep/app.py`, both `record_request_sample(session, key_id=key_id, ...)` calls (in `chat_completions` and `messages`) gain `account_id=account_id` (the local already read in Task 2).

- [ ] **Step 6: Run tests to confirm pass**

Run: `pytest tests/test_samples.py tests/test_request_samples_wiring.py tests/test_endpoint.py tests/test_messages_endpoint.py -q`
Expected: PASS.

- [ ] **Step 7: Write migration 0016**

Create `migrations/versions/0016_request_samples_account_id.py` - identical shape to 0015 but for `request_samples` (add nullable, backfill from `key_id -> api_keys.account_id`, alter NOT NULL, add FK `fk_request_samples_account_id`). No extra index (samples are queried by `prompt_name`, not account). `down_revision = "0015"`.

- [ ] **Step 8: Run full suite and commit**

```bash
pytest -q && ruff check . && ruff format --check .
git add gatekeep/models.py gatekeep/samples.py gatekeep/app.py migrations/versions/0016_request_samples_account_id.py tests/test_samples.py
git commit -m "feat: denormalize account_id onto request_samples"
```

---

### Task 4: Partition both response caches per account (decision 1)

Add `account_id` to `cached_responses`, swap its unique constraint to `(account_id, exact_hash)`, filter `find_semantic_match` by account, and **also** partition the Redis exact cache by account. The Redis exact cache (`cache_exact.py`) is keyed only by request hash today, so a caller's completion can be served verbatim to another tenant through it; decision 1's stated goal ("no cross-tenant content leakage") is not met unless it is partitioned too. This closes a gap the decision text left implicit (it names only `cached_responses`).

Existing `cached_responses` rows have no derivable owner (the table carries no `key_id`), so the migration deletes them; the cache is disposable (TTL'd, wiped on every prompt promotion) so this only costs a cold cache after deploy.

**Files:**
- Modify: `gatekeep/models.py` (`CachedResponse.account_id`, unique swap)
- Modify: `gatekeep/middleware/cache_semantic.py` (`store_cached_response`, `find_semantic_match`)
- Modify: `gatekeep/middleware/cache_exact.py` (account-scoped Redis keys + by-prompt index)
- Modify: `gatekeep/app.py` (thread `account_id` into cache read/write/lookup at both endpoints)
- Create: `migrations/versions/0017_cached_responses_account_id.py`
- Test: `tests/test_cache_semantic.py`, `tests/test_cache_exact.py`

**Interfaces:**
- Consumes: `Account` (Task 1), `account_id = key.account_id` (Task 2).
- Produces: `store_cached_response(session, *, account_id, exact_hash, ...) -> CachedResponse | None`.
- Produces: `find_semantic_match(session, embedding, *, account_id, model, threshold, max_age_seconds, prompt_version_num=None) -> SemanticMatch | None`.
- Produces (cache_exact): `hash_request` unchanged; `get_cached_response(redis, account_id, request_hash)`, `set_cached_response(redis, account_id, request_hash, response, *, ttl_seconds, prompt_name=None)`, `clear_cached_response(redis, account_id, request_hash)`, `invalidate_prompt_cache(redis, prompt_name)` unchanged signature (see below).
- Produces: `CachedResponse.account_id: int` (non-null); unique `(account_id, exact_hash)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cache_semantic.py` a test that two accounts do not share a semantic hit:

```python
@pytest.mark.asyncio
async def test_find_semantic_match_is_account_scoped(session):
    a1 = await create_account(session, name="a1")
    a2 = await create_account(session, name="a2")
    await session.commit()
    emb = [0.1] * EMBEDDING_DIM
    await store_cached_response(
        session,
        account_id=a1.id,
        exact_hash="h-a1",
        user_messages_text="hi",
        embedding=emb,
        response_text="secret-a1",
        model="claude-sonnet-5",
        cost_usd=0.01,
    )
    # Same account: hit.
    hit = await find_semantic_match(
        session, emb, account_id=a1.id, model="claude-sonnet-5",
        threshold=0.5, max_age_seconds=3600,
    )
    assert hit is not None and hit.cached.response_text == "secret-a1"
    # Other account: miss.
    miss = await find_semantic_match(
        session, emb, account_id=a2.id, model="claude-sonnet-5",
        threshold=0.5, max_age_seconds=3600,
    )
    assert miss is None
```

Add to `tests/test_cache_exact.py` a test that `set`/`get` are account-scoped (two accounts, same request hash, isolated):

```python
@pytest.mark.asyncio
async def test_exact_cache_is_account_scoped(fake_redis):  # use the module's redis fixture
    resp = _some_chat_completion_response()  # reuse the file's existing builder
    await set_cached_response(fake_redis, 1, "hash-x", resp, ttl_seconds=60)
    assert await get_cached_response(fake_redis, 1, "hash-x") is not None
    assert await get_cached_response(fake_redis, 2, "hash-x") is None
```

Match the existing Redis fixture/response-builder patterns already in `tests/test_cache_exact.py`.

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_cache_semantic.py::test_find_semantic_match_is_account_scoped tests/test_cache_exact.py::test_exact_cache_is_account_scoped -v`
Expected: FAIL (`TypeError` on the new `account_id` args).

- [ ] **Step 3: Add the column + unique swap**

In `gatekeep/models.py`, add to `CachedResponse`:

```python
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
```

Change `exact_hash` to drop its inline `unique=True`:

```python
    exact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
```

and add to `CachedResponse.__table_args__` a composite unique + keep the existing index:

```python
        UniqueConstraint("account_id", "exact_hash", name="uq_cached_responses_account_id_exact_hash"),
```

- [ ] **Step 4: Scope the semantic cache**

In `gatekeep/middleware/cache_semantic.py`:
- `store_cached_response`: add `account_id: int` (first keyword after `session`), set it on `CachedResponse(...)`. The existing `IntegrityError` -> rollback -> return None path now also covers a duplicate `(account_id, exact_hash)`, which is still the correct best-effort behavior.
- `find_semantic_match`: add required `account_id: int` keyword; add `.where(CachedResponse.account_id == account_id)` to `stmt`. Update the docstring to note matches never cross accounts.

- [ ] **Step 5: Scope the exact cache**

In `gatekeep/middleware/cache_exact.py`, make the Redis key account-scoped and keep prompt-invalidation working across accounts by storing the composite in the by-prompt index:

```python
def _redis_key(account_id: int, request_hash: str) -> str:
    """Build the namespaced, account-scoped Redis key for a request hash.

    Partitioning by account (decision 1) keeps one tenant's exact-cache hit
    from ever being served to another.
    """
    return f"{_KEY_PREFIX}{account_id}:{request_hash}"


def _member(account_id: int, request_hash: str) -> str:
    """The (account_id, request_hash) pair stored in a prompt's invalidation set."""
    return f"{account_id}:{request_hash}"


async def get_cached_response(
    redis: Redis, account_id: int, request_hash: str
) -> ChatCompletionResponse | None:
    """Look up a cached response for `account_id` by request hash, or None on a miss."""
    raw = await redis.get(_redis_key(account_id, request_hash))
    if raw is None:
        return None
    return ChatCompletionResponse.model_validate_json(raw)


async def set_cached_response(
    redis: Redis,
    account_id: int,
    request_hash: str,
    response: ChatCompletionResponse,
    *,
    ttl_seconds: int,
    prompt_name: str | None = None,
) -> None:
    """Store a response in the account's exact cache with a TTL.

    If `prompt_name` is set, the (account_id, request_hash) pair is added to a
    per-prompt set so a later promotion can invalidate every entry it produced,
    across all accounts.
    """
    await redis.set(
        _redis_key(account_id, request_hash), response.model_dump_json(), ex=ttl_seconds
    )
    if prompt_name is not None:
        await redis.sadd(_by_prompt_key(prompt_name), _member(account_id, request_hash))


async def clear_cached_response(redis: Redis, account_id: int, request_hash: str) -> None:
    """Manually invalidate one cached response for `account_id` by request hash."""
    await redis.delete(_redis_key(account_id, request_hash))


async def invalidate_prompt_cache(redis: Redis, prompt_name: str) -> None:
    """Delete every exact-cache entry tagged with `prompt_name`, across all accounts.

    Prompt promotion is a global operator action (decision 2), so this spans
    tenants. The invalidation set now stores `account_id:request_hash` members;
    each maps back to its account-scoped Redis key.
    """
    index_key = _by_prompt_key(prompt_name)
    members = await redis.smembers(index_key)
    if members:
        await redis.delete(*(f"{_KEY_PREFIX}{m}" for m in members))
    await redis.delete(index_key)
```

- [ ] **Step 6: Thread `account_id` through the app cache paths**

In `gatekeep/app.py`, at both endpoints:
- `get_cached_response(redis, request_hash)` -> `get_cached_response(redis, account_id, request_hash)`.
- `set_cached_response(redis, request_hash, response, ...)` -> `set_cached_response(redis, account_id, request_hash, response, ...)`.
- `find_semantic_match(session, embedding, model=..., ...)` -> add `account_id=account_id`.
- `store_cached_response(session, exact_hash=..., ...)` -> add `account_id=account_id`.

Also check for any other caller of these four via:

```bash
grep -rn "get_cached_response(\|set_cached_response(\|clear_cached_response(\|find_semantic_match(\|store_cached_response(" gatekeep/
```

`clear_cached_response` / `invalidate_prompt_cache` are called from prompt-promotion code (`gatekeep/prompts.py`); `invalidate_prompt_cache`'s signature is unchanged, so promotion needs no edit, but verify `clear_cached_response` has no callers that now need an `account_id` (if it does, thread it; if it has none, leave it).

- [ ] **Step 7: Run cache tests**

Run: `pytest tests/test_cache_semantic.py tests/test_cache_exact.py tests/test_prompts.py tests/test_endpoint.py tests/test_messages_endpoint.py -q`
Expected: PASS. Update any existing cache test that called the old signatures to pass an `account_id`.

- [ ] **Step 8: Write migration 0017**

Create `migrations/versions/0017_cached_responses_account_id.py`:

```python
"""cached_responses.account_id + per-account exact_hash uniqueness (decision 1)

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14

Existing rows have no derivable owner (no key_id on the table) and the cache is
disposable (TTL'd, wiped on promotion), so they are deleted rather than
backfilled. Redis exact-cache keys are re-namespaced by the application; stale
global keys simply age out.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM cached_responses")
    op.add_column(
        "cached_responses", sa.Column("account_id", sa.Integer(), nullable=False)
    )
    op.create_foreign_key(
        "fk_cached_responses_account_id",
        "cached_responses",
        "accounts",
        ["account_id"],
        ["id"],
    )
    op.drop_constraint("cached_responses_exact_hash_key", "cached_responses", type_="unique")
    op.create_unique_constraint(
        "uq_cached_responses_account_id_exact_hash",
        "cached_responses",
        ["account_id", "exact_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_cached_responses_account_id_exact_hash", "cached_responses", type_="unique"
    )
    op.create_unique_constraint(
        "cached_responses_exact_hash_key", "cached_responses", ["exact_hash"]
    )
    op.drop_constraint(
        "fk_cached_responses_account_id", "cached_responses", type_="foreignkey"
    )
    op.drop_column("cached_responses", "account_id")
```

(Confirm the existing unique constraint name with `\d cached_responses`; `cached_responses_exact_hash_key` is the Postgres default.)

- [ ] **Step 9: Run full suite and commit**

```bash
pytest -q && ruff check . && ruff format --check .
git add gatekeep/models.py gatekeep/middleware/cache_semantic.py gatekeep/middleware/cache_exact.py gatekeep/app.py migrations/versions/0017_cached_responses_account_id.py tests/test_cache_semantic.py tests/test_cache_exact.py
git commit -m "feat: partition response caches per account"
```

---

### Task 5: Budget + rate limit pooled at the account (decision 5)

Move the monthly spend cap and the rate-limit bucket from per-key to per-account. Budget enforcement reads `Account.monthly_budget_usd`, keys its Redis spend counter and its DB fallback aggregate by `account_id`, and `record_spend` increments the account counter. Rate limiting keys its token bucket by `account_id`. The per-key `api_keys.monthly_budget_usd` column is dropped. The `set-budget` CLI targets an account. With one-account-per-key from Task 1, this reproduces today's behavior exactly.

**Files:**
- Modify: `gatekeep/models.py` (drop `ApiKey.monthly_budget_usd`)
- Modify: `gatekeep/middleware/budget.py` (account-keyed spend, `require_budget` loads the account)
- Modify: `gatekeep/middleware/ratelimit.py` (bucket keyed by `account_id`)
- Modify: `gatekeep/accounting.py` (`record_spend` by `account_id`)
- Modify: `gatekeep/cli.py` (`_set_budget` + `set-budget` arg targets account)
- Create: `migrations/versions/0018_budget_ratelimit_account.py`
- Test: `tests/test_budget.py`, `tests/test_ratelimit.py`, `tests/test_cli.py`, `tests/test_accounting.py`

**Interfaces:**
- Consumes: `Account.monthly_budget_usd`, `ApiKey.account_id`, `RequestLog.account_id` (Tasks 1, 2).
- Produces: `record_spend(redis, *, account_id, cost_usd, now=None) -> float`.
- Produces: `get_period_spend(session, redis, *, account_id, now=None) -> float`.
- Produces: `check_budget(session, redis, account: Account, alert_threshold=None, now=None) -> tuple[bool, float | None]`.
- Produces: `check_rate_limit(redis, account_id, capacity, refill_rate, now=None) -> tuple[bool, float]`.
- Produces: `_set_budget(account_name, amount, unlimited)` (CLI now looks up an `Account` by name).

- [ ] **Step 1: Write the failing tests**

In `tests/test_budget.py`, adapt the existing budget test to put the cap on the account and assert enforcement uses the account pool. Add:

```python
@pytest.mark.asyncio
async def test_budget_pools_across_keys_in_one_account(session):
    account = await create_account(session, monthly_budget_usd=1.0)
    k1 = await create_key(session, account, name="k1", key_hash="bk1")
    k2 = await create_key(session, account, name="k2", key_hash="bk2")
    await session.commit()

    redis = get_redis()
    # Spend under k1 counts against the shared account pool k2 also draws on.
    await record_spend(redis, account_id=account.id, cost_usd=0.6)
    spent = await get_period_spend(session, redis, account_id=account.id)
    assert spent == pytest.approx(0.6)
    allowed, _ = await check_budget(session, redis, account)
    assert allowed is True  # 0.6 < 1.0
    await record_spend(redis, account_id=account.id, cost_usd=0.6)
    allowed, spent = await check_budget(session, redis, account)
    assert allowed is False  # 1.2 >= 1.0, regardless of which key spent it
```

In `tests/test_ratelimit.py`, adapt the existing test so two keys in one account share a bucket (consuming the account's tokens), and keys in different accounts do not. Follow the file's existing `check_rate_limit` call pattern, passing `account_id` where `key_id` was passed.

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_budget.py tests/test_ratelimit.py -q`
Expected: FAIL (signatures changed; `record_spend`/`check_budget` still key by `key_id`).

- [ ] **Step 3: Rekey rate limiting by account**

In `gatekeep/middleware/ratelimit.py`:
- `check_rate_limit(redis, key_id, ...)` -> `check_rate_limit(redis, account_id, ...)`; change the Lua bucket key to `f"ratelimit:{account_id}"`.
- `require_rate_limit`: use `key.account_id` in place of `key.id` when calling `check_rate_limit`. (No extra query - `account_id` is a column on the returned `ApiKey`.)

Update the teardown flush namespace in `tests/conftest.py` only if the prefix changed; it did not (`ratelimit:*` still matches).

- [ ] **Step 4: Rekey budget by account**

In `gatekeep/middleware/budget.py`:
- Rename the Redis-key helpers to take `account_id` (`_spend_redis_key(account_id, period)` -> `f"budget:spend:{account_id}:{period}"`; same for `_alert_redis_key`).
- `record_spend(redis, *, key_id, ...)` -> `record_spend(redis, *, account_id, ...)`.
- `_aggregate_spend_from_db`: filter `RequestLog.account_id == account_id` instead of `key_id`.
- `get_period_spend(..., *, key_id, ...)` -> `get_period_spend(..., *, account_id, ...)`.
- `check_budget(session, redis, key: ApiKey, ...)` -> `check_budget(session, redis, account: Account, ...)`; read `account.monthly_budget_usd` and pass `account.id` through. Update `_maybe_alert` to take/log `account_id`.
- `require_budget`: after the rate-limit dependency yields the `ApiKey`, load the account and check it:

```python
async def require_budget(
    key: ApiKey = Depends(require_rate_limit),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    """FastAPI dependency enforcing the account's monthly USD spend cap.

    Loads the caller's Account (the shared budget pool, decision 5) and
    raises HTTPException(429) once the account's current-period spend reaches
    its cap. Returns the ApiKey unchanged so downstream handlers keep the
    same dependency contract.
    """
    redis = get_redis(get_settings())
    account = await session.get(Account, key.account_id)
    allowed, spent = await check_budget(session, redis, account)
    if not allowed:
        raise _budget_exceeded(account.monthly_budget_usd, spent)
    return key
```

Import `Account` in `budget.py`.

- [ ] **Step 5: Rekey the accounting spend increment**

In `gatekeep/accounting.py`, `log_request` currently calls `record_spend(get_redis(), key_id=key_id, ...)`. Change to `record_spend(get_redis(), account_id=account_id, cost_usd=0.0 if cached else cost_usd)` (the `account_id` param added in Task 2 is in scope here). Update the surrounding best-effort-warning log's `extra` to `{"account_id": account_id}`.

- [ ] **Step 6: Drop `ApiKey.monthly_budget_usd` from the model**

In `gatekeep/models.py`, remove the `monthly_budget_usd` column from `ApiKey` (and from `tests/helpers.py::create_key`'s kwargs; update the two `tests/test_cli.py` keys that set it - move the budget onto the account instead).

- [ ] **Step 7: Point the CLI `set-budget` at an account**

In `gatekeep/cli.py`, change `_set_budget` to look up an `Account` by name (`select(Account).where(Account.name == name)`) and set `account.monthly_budget_usd`; update its docstring and the `set-budget` help text to say "account" not "key". Update `tests/test_cli.py`'s budget tests to create an account of that name.

- [ ] **Step 8: Run the affected suites**

Run: `pytest tests/test_budget.py tests/test_ratelimit.py tests/test_accounting.py tests/test_cli.py tests/test_endpoint.py -q`
Expected: PASS.

- [ ] **Step 9: Write migration 0018**

Create `migrations/versions/0018_budget_ratelimit_account.py`. The account budget was already backfilled from the key in migration 0014, so this migration only drops the now-dead per-key column:

```python
"""drop api_keys.monthly_budget_usd (budget pooled at the account, decision 5)

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-14

The account budget pool was seeded from each key's cap in migration 0014, so
the per-key column is now redundant and is removed. Rate limiting moves to an
account-keyed Redis bucket, which is application state with no schema change.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("api_keys", "monthly_budget_usd")


def downgrade() -> None:
    op.add_column(
        "api_keys", sa.Column("monthly_budget_usd", sa.Float(), nullable=True)
    )
    op.execute(
        """
        UPDATE api_keys SET monthly_budget_usd = accounts.monthly_budget_usd
        FROM accounts WHERE api_keys.account_id = accounts.id
        """
    )
```

- [ ] **Step 10: Run full suite and commit**

```bash
pytest -q && ruff check . && ruff format --check .
git add gatekeep/models.py gatekeep/middleware/budget.py gatekeep/middleware/ratelimit.py gatekeep/accounting.py gatekeep/cli.py migrations/versions/0018_budget_ratelimit_account.py tests/
git commit -m "feat: pool budget and rate limiting at the account"
```

---

### Task 6: Eval-case account provenance tag (decision 3)

Add a **nullable** `account_id` to `eval_cases` and have `curate_cases` tag each curated case with the account of the sample it was mined from. Manual cases (added via CLI/fixtures) legitimately have no account, hence nullable. The gate stays a single shared gate; only provenance is recorded.

**Files:**
- Modify: `gatekeep/models.py` (`EvalCase.account_id`, nullable)
- Modify: `gatekeep/evals.py` (`add_case` accepts optional `account_id`)
- Modify: `gatekeep/curation.py` (`curate_cases` passes `sample.account_id`)
- Create: `migrations/versions/0019_eval_cases_account_id.py`
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes: `RequestSample.account_id` (Task 3).
- Produces: `EvalCase.account_id: int | None` (nullable).
- Produces: `add_case(suite_id, session, *, input_messages, check_type, ..., account_id: int | None = None) -> EvalCase`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_curation.py` (the file already builds a key + samples; reuse that setup, now via the helpers so the sample carries an account):

```python
@pytest.mark.asyncio
async def test_curated_cases_carry_sample_account(session):
    account = await create_account(session)
    key = await create_key(session, account, key_hash="cur")
    await session.commit()
    # ... create a suite for the prompt and record >=1 request sample with
    # account_id=account.id via record_request_sample (mirror the file's
    # existing sample-creation), then:
    cases = await curate_cases(
        prompt_name, session, limit=5, provider=fake_provider, generate_model="m"
    )
    assert cases and all(c.account_id == account.id for c in cases)
```

Follow the file's existing fake-provider/suite scaffolding; the assertion is the new part.

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_curation.py::test_curated_cases_carry_sample_account -v`
Expected: FAIL (`account_id` is always None / attribute missing).

- [ ] **Step 3: Add the nullable column**

In `gatekeep/models.py`, add to `EvalCase`:

```python
    # The account whose sample this case was curated from (decision 3).
    # NULL for manually authored cases, which have no originating tenant.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
```

- [ ] **Step 4: Thread account_id through `add_case` and `curate_cases`**

In `gatekeep/evals.py`, `add_case`: add `account_id: int | None = None` keyword, set it on the `EvalCase(...)`. Update its docstring.

In `gatekeep/curation.py`, `curate_cases`: pass `account_id=sample.account_id` into the `add_case(...)` call inside the loop (each curated case inherits its source sample's account).

- [ ] **Step 5: Run to confirm pass**

Run: `pytest tests/test_curation.py tests/test_evals.py tests/test_eval_models.py -q`
Expected: PASS.

- [ ] **Step 6: Write migration 0019**

Create `migrations/versions/0019_eval_cases_account_id.py` - add a **nullable** `account_id` with an FK, no backfill (existing cases stay NULL), no NOT NULL tightening:

```python
"""eval_cases.account_id provenance tag (decision 3)

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("eval_cases", sa.Column("account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_eval_cases_account_id", "eval_cases", "accounts", ["account_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_eval_cases_account_id", "eval_cases", type_="foreignkey")
    op.drop_column("eval_cases", "account_id")
```

- [ ] **Step 7: Run full suite and commit**

```bash
pytest -q && ruff check . && ruff format --check .
git add gatekeep/models.py gatekeep/evals.py gatekeep/curation.py migrations/versions/0019_eval_cases_account_id.py tests/test_curation.py
git commit -m "feat: tag curated eval cases with source account"
```

---

### Task 7: Account-scoped dashboard + `is_operator` gating (problem 1, decision 6)

The payoff task. Every dashboard read is scoped to the caller's account unless the caller's account has `is_operator = true`. This closes the isolation hole in problem 1 (any key could read any other key's usage via a client-supplied `key_id`) and gates the fleet-wide cross-account view behind the operator flag (decision 6). The `key_id` query filter stays but is ANDed with the account scope, so a non-operator passing another account's `key_id` gets an empty result rather than a leak.

**Files:**
- Modify: `gatekeep/api/dashboard.py` (load caller account; account-scope every query; gate cross-account view)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `RequestLog.account_id` (Task 2), `Account.is_operator`, `ApiKey.account_id` (Task 1).
- Produces: `_account_scope(caller_account: Account) -> list` helper returning `[RequestLog.account_id == caller_account.id]` for a non-operator, or `[]` for an operator.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard.py` (the file already exercises the endpoints via the app + a real key; reuse that harness):

```python
@pytest.mark.asyncio
async def test_usage_summary_scopes_to_caller_account(...):
    # Two accounts, each with a key and one request_log row (distinct cost).
    # Calling /dashboard/api/usage/summary with account A's key returns only
    # A's totals; A's by_key breakdown contains only A's key.
    ...
    assert body["cost_usd"] == pytest.approx(a_cost)  # not a_cost + b_cost
    assert {row["key"] for row in body["by_key"]} == {str(a_key_id)}


@pytest.mark.asyncio
async def test_operator_sees_fleet_wide(...):
    # A key on an account with is_operator=True sees both accounts' totals.
    assert body["cost_usd"] == pytest.approx(a_cost + b_cost)
    assert {row["key"] for row in body["by_key"]} == {str(a_key_id), str(b_key_id)}


@pytest.mark.asyncio
async def test_non_operator_cannot_read_other_account_by_key_id(...):
    # Account A's key passing ?key_id=<B's key id> gets empty totals, not B's.
    assert body["request_count"] == 0
```

Follow the file's existing request-issuing pattern (it already seeds `RequestLog` rows and calls the endpoints); the new part is seeding two accounts and asserting scope.

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/test_dashboard.py -k "scope or operator or other_account" -v`
Expected: FAIL (queries are unscoped; totals include both accounts).

- [ ] **Step 3: Load the caller's account and add the scope helper**

In `gatekeep/api/dashboard.py`, import `Account`, and add:

```python
def _account_scope(caller_account: Account) -> list:
    """Return the WHERE clauses restricting a query to the caller's account.

    A non-operator account sees only its own rows (decision 6); an operator
    account sees the whole fleet, so this returns no clause. account_id is
    always the caller's own, derived server-side - never client-supplied.
    """
    if caller_account.is_operator:
        return []
    return [RequestLog.account_id == caller_account.id]
```

Add a small dependency that resolves the caller's `Account` from the authenticated key, reused by every endpoint:

```python
async def _require_caller_account(
    caller: ApiKey = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> Account:
    """Resolve the authenticated key's Account, for account-scoped dashboards."""
    return await session.get(Account, caller.account_id)
```

- [ ] **Step 4: Apply the scope in every endpoint**

For each dashboard endpoint (`usage_summary`, `usage_timeseries`, `usage_timeseries_by_model`, `latency_summary`, `latency_timeseries`), replace the `_caller: ApiKey = Depends(require_api_key)` parameter with `caller_account: Account = Depends(_require_caller_account)`, and extend the `filters` list with the scope. Concretely, after building `filters`:

```python
    filters = _base_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name)
    filters += _account_scope(caller_account)
```

and for the two latency endpoints, `filters` comes from `_latency_filters(...)`; append `_account_scope(caller_account)` the same way. Because `_key_breakdown` / `_latency_key_breakdown` and every aggregate build on `filters`, the account scope flows into all of them, so the cross-account `by_key` breakdown (`dashboard.py` `_key_breakdown`) automatically narrows to the caller's account for non-operators and stays fleet-wide for operators - exactly the "survives but gates behind is_operator" behavior decision 6 specifies. No change to `_key_breakdown` itself is required.

Leave `eval_history`, `list_prompts_dashboard`, and `prompt_version_timeline` on `require_api_key`: prompts and eval suites are global operator-managed data (decisions 2, 3), not tenant-scoped, so those endpoints are not account-scoped.

- [ ] **Step 5: Run to confirm pass**

Run: `pytest tests/test_dashboard.py -q`
Expected: PASS. Update any pre-existing dashboard test that assumed unscoped totals to seed its rows under the calling key's account (or to use an operator account when it means to read fleet-wide).

- [ ] **Step 6: Run full suite and commit**

```bash
pytest -q && ruff check . && ruff format --check .
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat: scope dashboard reads to the caller account, gate fleet view on is_operator"
```

---

## Self-Review

**Spec coverage:**

| Spec item | Task |
|---|---|
| Accounts table; `ApiKey.account_id` FK; keys as access method | 1 |
| Decision 1 - cache partitioned per account (`cached_responses` + Redis exact cache) | 4 |
| Decision 2 - prompts stay global (no `account_id`) | none needed (verified: prompt endpoints left unscoped in Task 7) |
| Decision 3 - shared gate, account-tagged eval cases | 6 |
| Decision 4 - `request_samples.account_id` denormalized | 3 |
| Decision 5 - budget + rate limit pooled at account; per-key budget dropped | 1 (column) + 5 (enforcement) |
| Decision 6 - single `is_operator` flag gates fleet-wide dashboard | 1 (column) + 7 (gating) |
| Decision 7 - `ApiKey.name` unique per `(account_id, name)` | 1 |
| Decision 8 - one account per key, nullable -> backfill -> NOT NULL | 1 (migration 0014) |
| Decision 9 - `request_logs.account_id` direct column | 2 |
| Problem 1 - dashboard no longer readable across keys via client-supplied `key_id` | 7 |
| Problem 2 - ambiguous duplicate key names | 1 (per-account uniqueness) |

**Gaps deliberately surfaced (not silent):**
- Redis exact cache partitioning is required by decision 1's stated goal but not named in its text; folded into Task 4 and flagged.
- `cached_responses` existing rows have no derivable owner - deleted at migration (Task 4), cache is disposable.
- "rate-limit config on accounts" (tables list) is interpreted as pooling-only per decision 5's body; documented in Global Constraints.
- No new account/key-creation admin endpoint or CLI `key create` command exists or is added - out of spec scope. Accounts are created by migration 0014; new tenants are provisioned by whatever process already mints keys (tests create them directly). If operators need a self-serve "create account/key" path, that is a follow-up spec.

**Type consistency:** `account_id: int` is the parameter name everywhere it is written (`log_request`, `record_request_sample`, `store_cached_response`, `find_semantic_match`, `record_spend`, `get_period_spend`, `check_rate_limit`, `add_case`); `check_budget` takes an `Account` object; `require_budget` returns `ApiKey`; dashboard endpoints depend on `_require_caller_account -> Account`. Migration revisions chain `0013 -> 0014 -> ... -> 0019`. Helper names `create_account` / `create_key` are used consistently across all task tests.
