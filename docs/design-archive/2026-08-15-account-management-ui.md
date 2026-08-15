# Account Management UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a management surface (service layer + API + CLI + React UI) for accounts and API keys on top of the already-implemented multi-tenancy data layer.

**Architecture:** A new `gatekeep/account_service.py` holds all account/key business logic as plain async functions that own their own commit and translate DB uniqueness violations into typed errors. Both the CLI (`gatekeep/cli.py`) and the API (`gatekeep/api/dashboard.py`) call that one service layer, so there is a single tested code path and no duplicated logic. The React `dashboard/` app gains a header tab toggle and a `ManagementPage` that talks to the new routes.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy (async), Pydantic, Redis (async), Postgres; pytest + pytest-asyncio (`asyncio_mode = "auto"`); React + TypeScript + Tailwind (Vite) with no frontend test runner.

**Spec:** `docs/design-archive/2026-08-15-account-management-ui-design.md`

## Global Constraints

- **Docstrings required** on every new function, method, and class (Python standard docstrings; JSDoc-style `/** ... */` for TS). State purpose, parameters, return values, and exceptions where applicable.
- **Never use the em dash "-".** Use a plain dash `-`.
- **No self-added co-author** lines in commit messages.
- **Error bodies follow the existing OpenAI-shaped convention:** `{"error": {"message": ..., "type": ..., "code": None}}`, built the same way `gatekeep/api/errors.py::openai_error` and `middleware/auth.py::_unauthorized` build them.
- **Never call `middleware.budget.check_budget` on a dashboard read** - it fires budget alerts and increments the `budget_alerts_total` Prometheus counter. Use `middleware.budget.get_period_spend(session, redis, account_id=...)` for month-to-date spend.
- **Budget validation:** `monthly_budget_usd` must be strictly positive, or explicitly `None` (cleared). Non-positive values are rejected (`422` at the API, message at the CLI), matching the existing CLI validation in `cli.py::_set_budget`.
- **Authz rule** mirrors `dashboard.py::_account_scope`: an operator (`is_operator = true`) may target any `account_id`; a non-operator may target only its own account, else `403`. Budget changes and all-account management are operator-only.
- `account_id` is derived from the authenticated key for "own account" checks - never trusted from the client for the tier decision.
- **Test DB:** tests need Postgres + Redis running and `TEST_DATABASE_URL` distinct from `DATABASE_URL` (see `tests/conftest.py`). On this machine there is no host `psql`; scripts must not depend on it.
- **Run the Python suite with:** `pytest <path> -v` from the repo root.
- **Run ruff before each commit:** `ruff format . && ruff check .`.

### Resolved open questions (from the spec)

1. `key set-budget` -> `account set-budget`: **moved outright, no back-compat alias.** The `key` subcommand group loses `set-budget` entirely; budget lives only under `account`.
2. `GET /accounts` (operator) returns each account's `created_at`, `active_key_count`, **and** `total_key_count` (active + revoked), plus `monthly_budget_usd`, `is_operator`, and `spend_mtd`.

---

## File Structure

**New backend files:**
- `gatekeep/account_service.py` - all account/key business logic + typed errors + the `AccountStats` return dataclass.
- `tests/test_account_service.py` - service-layer unit tests.

**Modified backend files:**
- `gatekeep/api/dashboard.py` - new `require_operator` dependency, a Redis dependency, and the seven management routes + their Pydantic response/request models.
- `gatekeep/cli.py` - new `account` subcommand group and expanded `key` subcommand group; `key set-budget` removed.
- `tests/test_dashboard.py` - route-level tests for the new endpoints.
- `tests/test_cli.py` - operator-bootstrap + key-create CLI tests.
- `scripts/create_key.py` - route through the service layer, take an account name, create the account when missing.
- `scripts/init-test-key.sh` - mint via `create_key.py` instead of raw `psql` INSERT.

**New frontend files:**
- `dashboard/src/pages/ManagementPage.tsx`
- `dashboard/src/components/BudgetCard.tsx`
- `dashboard/src/components/KeyTable.tsx`
- `dashboard/src/components/CreateKeyModal.tsx`
- `dashboard/src/components/AccountsTable.tsx`
- `dashboard/src/components/AccountDetailPanel.tsx`
- `dashboard/src/components/CreateAccountModal.tsx`

**Modified frontend files:**
- `dashboard/src/api/types.ts` - new request/response shapes.
- `dashboard/src/api/client.ts` - POST/PATCH helpers + management GET helpers.
- `dashboard/src/App.tsx` - own the caller `MeResponse` and the active tab.
- `dashboard/src/components/Header.tsx` - tab control.
- `dashboard/src/pages/DashboardPage.tsx` - accept the shared tab so Analytics stays one tab.

---

## Task 1: Service layer - typed errors and account write functions

**Files:**
- Create: `gatekeep/account_service.py`
- Test: `tests/test_account_service.py`

**Interfaces:**
- Consumes: `gatekeep.models.Account`, `gatekeep.models.ApiKey`; `sqlalchemy.exc.IntegrityError`.
- Produces (later tasks rely on these exact names/signatures):
  - Exceptions: `AccountServiceError` (base), `AccountNotFoundError`, `KeyNotFoundError`, `AccountNameConflictError`, `KeyNameConflictError`, `LastOperatorError`, `InvalidBudgetError`.
  - `async create_account(session, *, name: str, monthly_budget_usd: float | None = None, is_operator: bool = False) -> Account`
  - `async rename_account(session, account_id: int, new_name: str) -> Account`
  - `async set_budget(session, account_id: int, amount: float | None) -> Account`
  - `async set_operator(session, account_id: int, value: bool) -> Account`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_account_service.py`:

```python
from __future__ import annotations

import pytest

from gatekeep import account_service as svc
from gatekeep.models import Account
from tests.helpers import create_account


async def test_create_account_persists_and_returns_it(session):
    """create_account inserts the row, commits, and returns it with an id."""
    account = await svc.create_account(session, name="acme", monthly_budget_usd=10.0)
    assert account.id is not None
    assert account.name == "acme"
    assert account.monthly_budget_usd == 10.0
    assert account.is_operator is False
    fetched = await session.get(Account, account.id)
    assert fetched is not None


async def test_create_account_rejects_duplicate_name(session):
    """A globally-unique name collision raises AccountNameConflictError."""
    await svc.create_account(session, name="dup")
    with pytest.raises(svc.AccountNameConflictError):
        await svc.create_account(session, name="dup")


async def test_create_account_rejects_non_positive_budget(session):
    """A zero or negative budget raises InvalidBudgetError."""
    with pytest.raises(svc.InvalidBudgetError):
        await svc.create_account(session, name="cheap", monthly_budget_usd=0)


async def test_rename_account_changes_name(session):
    """rename_account updates the name and returns the account."""
    account = await create_account(session)
    await session.commit()
    renamed = await svc.rename_account(session, account.id, "renamed")
    assert renamed.name == "renamed"


async def test_rename_missing_account_raises(session):
    """rename_account on an unknown id raises AccountNotFoundError."""
    with pytest.raises(svc.AccountNotFoundError):
        await svc.rename_account(session, 999999, "whatever")


async def test_rename_to_taken_name_conflicts(session):
    """Renaming onto an existing name raises AccountNameConflictError."""
    a = await create_account(session, name="a")
    await create_account(session, name="b")
    await session.commit()
    with pytest.raises(svc.AccountNameConflictError):
        await svc.rename_account(session, a.id, "b")


async def test_set_budget_sets_and_clears(session):
    """set_budget stores a positive amount and clears it with None."""
    account = await create_account(session)
    await session.commit()
    await svc.set_budget(session, account.id, 25.0)
    assert (await session.get(Account, account.id)).monthly_budget_usd == 25.0
    await svc.set_budget(session, account.id, None)
    assert (await session.get(Account, account.id)).monthly_budget_usd is None


async def test_set_budget_rejects_non_positive(session):
    """set_budget with a non-positive amount raises InvalidBudgetError."""
    account = await create_account(session)
    await session.commit()
    with pytest.raises(svc.InvalidBudgetError):
        await svc.set_budget(session, account.id, -1.0)


async def test_set_operator_toggles(session):
    """set_operator flips the flag on when at least one other operator remains."""
    keeper = await create_account(session, name="keeper", is_operator=True)
    target = await create_account(session, name="target")
    await session.commit()
    await svc.set_operator(session, target.id, True)
    assert (await session.get(Account, target.id)).is_operator is True
    # keeper referenced so the fixture's operator is unambiguous
    assert keeper.is_operator is True


async def test_set_operator_last_operator_guard(session):
    """Turning off the only operator raises LastOperatorError."""
    only = await create_account(session, name="only", is_operator=True)
    await session.commit()
    with pytest.raises(svc.LastOperatorError):
        await svc.set_operator(session, only.id, False)


async def test_set_operator_off_allowed_with_another_operator(session):
    """Turning off one operator is allowed when another operator exists."""
    a = await create_account(session, name="op-a", is_operator=True)
    await create_account(session, name="op-b", is_operator=True)
    await session.commit()
    off = await svc.set_operator(session, a.id, False)
    assert off.is_operator is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_account_service.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'gatekeep.account_service'` (collection error).

- [ ] **Step 3: Write the module with errors and account write functions**

Create `gatekeep/account_service.py`:

```python
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import Account


class AccountServiceError(Exception):
    """Base class for account/key service errors that callers translate.

    The API maps subclasses to HTTP status codes and the CLI prints their
    message; raising one of these (rather than leaking a raw SQLAlchemy or
    ValueError) is what lets both callers handle failures identically.
    """


class AccountNotFoundError(AccountServiceError):
    """Raised when no account exists for a given id."""


class KeyNotFoundError(AccountServiceError):
    """Raised when no key with a given id exists on the target account."""


class AccountNameConflictError(AccountServiceError):
    """Raised when an account name collides with the global unique constraint."""


class KeyNameConflictError(AccountServiceError):
    """Raised when a key name collides within its account's namespace."""


class LastOperatorError(AccountServiceError):
    """Raised when clearing operator status would leave zero operators."""


class InvalidBudgetError(AccountServiceError):
    """Raised when a budget amount is present but not strictly positive."""


def _validate_budget(amount: float | None) -> None:
    """Reject a non-positive budget; None (unlimited/cleared) is always allowed.

    Raises:
        InvalidBudgetError: if `amount` is not None and not strictly positive.
    """
    if amount is not None and amount <= 0:
        raise InvalidBudgetError("budget amount must be positive")


async def _get_account_or_404(session: AsyncSession, account_id: int) -> Account:
    """Load an account by id or raise AccountNotFoundError."""
    account = await session.get(Account, account_id)
    if account is None:
        raise AccountNotFoundError(f"no account with id {account_id}")
    return account


async def create_account(
    session: AsyncSession,
    *,
    name: str,
    monthly_budget_usd: float | None = None,
    is_operator: bool = False,
) -> Account:
    """Create an account, commit, and return it.

    Args:
        session: Async DB session.
        name: Globally-unique account name.
        monthly_budget_usd: Positive spend cap, or None for unlimited.
        is_operator: Whether the account gets the fleet-wide operator view.

    Returns:
        The persisted Account with its id populated.

    Raises:
        InvalidBudgetError: if `monthly_budget_usd` is non-positive.
        AccountNameConflictError: if `name` is already taken.
    """
    _validate_budget(monthly_budget_usd)
    account = Account(
        name=name, monthly_budget_usd=monthly_budget_usd, is_operator=is_operator
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AccountNameConflictError(f"account name {name!r} is already taken") from exc
    return account


async def rename_account(session: AsyncSession, account_id: int, new_name: str) -> Account:
    """Rename an account, commit, and return it.

    Raises:
        AccountNotFoundError: if no account has that id.
        AccountNameConflictError: if `new_name` is already taken.
    """
    account = await _get_account_or_404(session, account_id)
    account.name = new_name
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AccountNameConflictError(
            f"account name {new_name!r} is already taken"
        ) from exc
    return account


async def set_budget(session: AsyncSession, account_id: int, amount: float | None) -> Account:
    """Set or clear an account's monthly spend cap, commit, and return it.

    Args:
        amount: Positive cap, or None to clear it (unlimited).

    Raises:
        AccountNotFoundError: if no account has that id.
        InvalidBudgetError: if `amount` is present but non-positive.
    """
    _validate_budget(amount)
    account = await _get_account_or_404(session, account_id)
    account.monthly_budget_usd = amount
    await session.commit()
    return account


async def set_operator(session: AsyncSession, account_id: int, value: bool) -> Account:
    """Set an account's operator flag, guarding against removing the last operator.

    Args:
        value: The new operator flag.

    Raises:
        AccountNotFoundError: if no account has that id.
        LastOperatorError: if setting `value` False would leave zero operators.
    """
    account = await _get_account_or_404(session, account_id)
    if account.is_operator and not value:
        other_operators = (
            await session.execute(
                select(func.count(Account.id)).where(
                    Account.is_operator.is_(True), Account.id != account_id
                )
            )
        ).scalar_one()
        if other_operators == 0:
            raise LastOperatorError("cannot remove the last operator")
    account.is_operator = value
    await session.commit()
    return account
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_account_service.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Lint and commit**

```bash
ruff format gatekeep/account_service.py tests/test_account_service.py
ruff check gatekeep/account_service.py tests/test_account_service.py
git add gatekeep/account_service.py tests/test_account_service.py
git commit -m "feat: add account_service account write functions and typed errors"
```

---

## Task 2: Service layer - key functions and account stats

**Files:**
- Modify: `gatekeep/account_service.py`
- Test: `tests/test_account_service.py`

**Interfaces:**
- Consumes: Task 1's `_get_account_or_404`, error classes; `gatekeep.auth_keys.generate_key` / `hash_key`; `gatekeep.middleware.budget.get_period_spend`; `gatekeep.models.ApiKey`; `redis.asyncio.Redis`.
- Produces:
  - `@dataclass AccountStats` with fields: `id: int`, `name: str`, `is_operator: bool`, `monthly_budget_usd: float | None`, `created_at: datetime`, `active_key_count: int`, `total_key_count: int`, `spend_mtd: float`.
  - `async list_keys(session, account_id: int) -> list[ApiKey]`
  - `async create_key(session, account_id: int, name: str) -> tuple[ApiKey, str]` (raw key is the second element; only its hash is stored)
  - `async revoke_key(session, account_id: int, key_id: int) -> ApiKey` (sets `active = False`)
  - `async list_accounts_with_stats(session, redis) -> list[AccountStats]`
  - `async get_account_spend(session, redis, account_id: int) -> float` (thin wrapper over `get_period_spend`, used by `GET /me`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_account_service.py`:

```python
from gatekeep.auth_keys import hash_key as _hash_key
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, RequestLog


async def test_create_key_returns_raw_and_stores_hash(session):
    """create_key returns the raw key once and persists only its sha256 hash."""
    account = await create_account(session)
    await session.commit()
    key, raw = await svc.create_key(session, account.id, "prod")
    assert raw.startswith("gk-")
    assert key.name == "prod"
    assert key.active is True
    assert key.account_id == account.id
    assert key.key_hash == _hash_key(raw)


async def test_create_key_duplicate_name_conflicts(session):
    """A duplicate key name within the same account raises KeyNameConflictError."""
    account = await create_account(session)
    await session.commit()
    await svc.create_key(session, account.id, "dup")
    with pytest.raises(svc.KeyNameConflictError):
        await svc.create_key(session, account.id, "dup")


async def test_create_key_missing_account_raises(session):
    """Minting for an unknown account raises AccountNotFoundError."""
    with pytest.raises(svc.AccountNotFoundError):
        await svc.create_key(session, 999999, "x")


async def test_list_keys_returns_account_keys(session):
    """list_keys returns exactly the target account's keys."""
    a = await create_account(session, name="a")
    b = await create_account(session, name="b")
    await session.commit()
    await svc.create_key(session, a.id, "a1")
    await svc.create_key(session, b.id, "b1")
    keys = await svc.list_keys(session, a.id)
    assert [k.name for k in keys] == ["a1"]


async def test_revoke_key_soft_revokes(session):
    """revoke_key flips active to False and leaves the row in place."""
    account = await create_account(session)
    await session.commit()
    key, _ = await svc.create_key(session, account.id, "prod")
    revoked = await svc.revoke_key(session, account.id, key.id)
    assert revoked.active is False
    assert await session.get(ApiKey, key.id) is not None


async def test_revoke_key_other_account_raises(session):
    """Revoking a key that belongs to another account raises KeyNotFoundError."""
    a = await create_account(session, name="a")
    b = await create_account(session, name="b")
    await session.commit()
    key, _ = await svc.create_key(session, b.id, "b1")
    with pytest.raises(svc.KeyNotFoundError):
        await svc.revoke_key(session, a.id, key.id)


async def test_list_accounts_with_stats_counts_and_spend(session):
    """Stats include active/total key counts and non-cached MTD spend."""
    redis = get_redis()
    await redis.flushdb()
    account = await create_account(session, name="acme", monthly_budget_usd=100.0)
    await session.commit()
    active, _ = await svc.create_key(session, account.id, "k-active")
    revoked, _ = await svc.create_key(session, account.id, "k-revoked")
    await svc.revoke_key(session, account.id, revoked.id)
    session.add(
        RequestLog(
            key_id=active.id,
            account_id=account.id,
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost_usd=3.0,
            cached=False,
            response_id="r1",
        )
    )
    await session.commit()

    stats = await svc.list_accounts_with_stats(session, redis)
    row = next(s for s in stats if s.name == "acme")
    assert row.active_key_count == 1
    assert row.total_key_count == 2
    assert row.spend_mtd == pytest.approx(3.0)
    assert row.monthly_budget_usd == 100.0
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_account_service.py -k "key or stats" -v`
Expected: FAIL - `AttributeError: module 'gatekeep.account_service' has no attribute 'create_key'`.

- [ ] **Step 3: Extend the module**

Add these imports at the top of `gatekeep/account_service.py` (merge with the existing import block):

```python
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.budget import get_period_spend
from gatekeep.models import Account, ApiKey
```

Append to `gatekeep/account_service.py`:

```python
@dataclass
class AccountStats:
    """An account row plus its key counts and month-to-date spend.

    `spend_mtd` is budget-relevant spend (non-cached provider cost for the
    current UTC calendar month), the same figure the budget cap enforces -
    deliberately lower than the Analytics tab's cost, which includes the
    notional cost of cache hits.
    """

    id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    created_at: datetime
    active_key_count: int
    total_key_count: int
    spend_mtd: float


async def list_keys(session: AsyncSession, account_id: int) -> list[ApiKey]:
    """Return an account's keys (active and revoked), newest first.

    Raises:
        AccountNotFoundError: if no account has that id.
    """
    await _get_account_or_404(session, account_id)
    rows = (
        (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.account_id == account_id)
                .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def create_key(
    session: AsyncSession, account_id: int, name: str
) -> tuple[ApiKey, str]:
    """Mint a key for an account, commit, and return (key, raw_key).

    The raw key is returned exactly once; only its sha256 hash is persisted.

    Raises:
        AccountNotFoundError: if no account has that id.
        KeyNameConflictError: if the name is already used on that account.
    """
    await _get_account_or_404(session, account_id)
    raw = generate_key()
    key = ApiKey(account_id=account_id, name=name, key_hash=hash_key(raw), active=True)
    session.add(key)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise KeyNameConflictError(
            f"key name {name!r} is already used on this account"
        ) from exc
    return key, raw


async def revoke_key(session: AsyncSession, account_id: int, key_id: int) -> ApiKey:
    """Soft-revoke a key (active = False) that belongs to `account_id`.

    Scoping the lookup by account_id is the guard that a revoke only ever
    affects a key on the authorized account.

    Raises:
        KeyNotFoundError: if no active-or-revoked key with that id exists on
            the account.
    """
    key = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.account_id == account_id)
        )
    ).scalar_one_or_none()
    if key is None:
        raise KeyNotFoundError(f"no key {key_id} on account {account_id}")
    key.active = False
    await session.commit()
    return key


async def get_account_spend(
    session: AsyncSession, redis: Redis, account_id: int
) -> float:
    """Return an account's current-period budget-relevant spend.

    Thin wrapper over `middleware.budget.get_period_spend` so callers never
    reach for `check_budget` (which would fire alerts on a dashboard read).
    """
    return await get_period_spend(session, redis, account_id=account_id)


async def list_accounts_with_stats(
    session: AsyncSession, redis: Redis
) -> list[AccountStats]:
    """Return every account with key counts and month-to-date spend, by name.

    Key counts come from one grouped aggregate over api_keys; spend comes
    from `get_period_spend` per account (Redis fast path, DB fallback).
    """
    accounts = (
        (await session.execute(select(Account).order_by(Account.name))).scalars().all()
    )
    count_rows = (
        await session.execute(
            select(
                ApiKey.account_id,
                func.count(ApiKey.id),
                func.coalesce(
                    func.sum(func.cast(ApiKey.active, sa_Integer)), 0
                ),
            ).group_by(ApiKey.account_id)
        )
    ).all()
    totals = {aid: int(total) for aid, total, _ in count_rows}
    actives = {aid: int(active) for aid, _, active in count_rows}

    stats: list[AccountStats] = []
    for account in accounts:
        spend = await get_period_spend(session, redis, account_id=account.id)
        stats.append(
            AccountStats(
                id=account.id,
                name=account.name,
                is_operator=account.is_operator,
                monthly_budget_usd=account.monthly_budget_usd,
                created_at=account.created_at,
                active_key_count=actives.get(account.id, 0),
                total_key_count=totals.get(account.id, 0),
                spend_mtd=spend,
            )
        )
    return stats
```

Add `Integer as sa_Integer` to the SQLAlchemy import in the top block so `func.cast(ApiKey.active, sa_Integer)` resolves:

```python
from sqlalchemy import Integer as sa_Integer, func, select
```

- [ ] **Step 4: Run the full service test file to verify it passes**

Run: `pytest tests/test_account_service.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Lint and commit**

```bash
ruff format gatekeep/account_service.py tests/test_account_service.py
ruff check gatekeep/account_service.py tests/test_account_service.py
git add gatekeep/account_service.py tests/test_account_service.py
git commit -m "feat: add account_service key management and account stats"
```

---

## Task 3: API - operator dependency, Redis dependency, and GET /me

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `dashboard.py::_require_caller_account`; `middleware.ratelimit.get_redis`; `config.get_settings`; Task 2's `get_account_spend`.
- Produces:
  - `_get_redis() -> Redis` dependency.
  - `require_operator(caller_account = Depends(_require_caller_account)) -> Account` (raises `403` when `not caller_account.is_operator`).
  - `MeResponse` model: `account_id: int`, `name: str`, `is_operator: bool`, `monthly_budget_usd: float | None`, `spend_mtd: float`.
  - `GET /dashboard/api/me` route.
  - `_forbidden(message: str) -> HTTPException` helper (OpenAI-shaped `403`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
from tests.helpers import create_key as _create_key


@pytest_asyncio.fixture
async def operator_key(session):
    """A raw active key on an operator account."""
    raw = generate_key()
    account = await create_account(session, name="op-acct", is_operator=True)
    session.add(ApiKey(name="op-key", key_hash=hash_key(raw), account_id=account.id))
    await session.commit()
    return raw


async def test_me_returns_caller_shape(client, raw_key):
    """GET /me returns the caller's account id, name, operator flag, budget, spend."""
    resp = await client.get(
        "/dashboard/api/me", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "account_id",
        "name",
        "is_operator",
        "monthly_budget_usd",
        "spend_mtd",
    }
    assert body["is_operator"] is False


async def test_me_requires_auth(client):
    """GET /me with no key is 401."""
    resp = await client.get("/dashboard/api/me")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "me_returns or me_requires" -v`
Expected: FAIL - `404` (route not defined) on the first test.

- [ ] **Step 3: Add the dependencies, models, and route**

In `gatekeep/api/dashboard.py`, add to the imports:

```python
from redis.asyncio import Redis

from gatekeep import account_service
from gatekeep.config import get_settings
from gatekeep.middleware.ratelimit import get_redis
```

Add near the top of the module (after `_require_caller_account` / `_account_scope`):

```python
def _get_redis() -> Redis:
    """FastAPI dependency yielding the shared async Redis client.

    Management routes need Redis for month-to-date spend via
    `middleware.budget.get_period_spend`; the analytics routes touch only
    Postgres, so this is scoped to the routes that need it.
    """
    return get_redis(get_settings())


def _forbidden(message: str) -> HTTPException:
    """Build a 403 HTTPException with an OpenAI-shaped error body."""
    return HTTPException(
        status_code=403,
        detail={"error": {"message": message, "type": "permission_error", "code": None}},
    )


async def require_operator(
    caller_account: Account = Depends(_require_caller_account),
) -> Account:
    """FastAPI dependency that authorizes only operator accounts.

    Builds on `_require_caller_account`; raises a 403 (OpenAI-shaped body)
    when the caller's account is not an operator.
    """
    if not caller_account.is_operator:
        raise _forbidden("Operator access required.")
    return caller_account
```

Add the model and route (near the other route definitions):

```python
class MeResponse(BaseModel):
    """The caller's own account context, driving tab visibility and the budget card."""

    account_id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    spend_mtd: float


@router.get("/me", response_model=MeResponse)
async def get_me(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(_get_redis),
    caller_account: Account = Depends(_require_caller_account),
) -> MeResponse:
    """Return the caller's account context: id, name, operator flag, budget
    cap, and current-period budget-relevant spend. Requires a valid API key.
    """
    spend = await account_service.get_account_spend(session, redis, caller_account.id)
    return MeResponse(
        account_id=caller_account.id,
        name=caller_account.name,
        is_operator=caller_account.is_operator,
        monthly_budget_usd=caller_account.monthly_budget_usd,
        spend_mtd=spend,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard.py -k "me_returns or me_requires" -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff format gatekeep/api/dashboard.py tests/test_dashboard.py
ruff check gatekeep/api/dashboard.py tests/test_dashboard.py
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat: add GET /me plus require_operator and redis dashboard dependencies"
```

---

## Task 4: API - account-scoped key routes (list / mint / revoke)

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 3's `_forbidden`, `require_operator` not needed here; `_require_caller_account`; Task 2's `list_keys` / `create_key` / `revoke_key` and its errors.
- Produces:
  - `_authorize_account_access(caller_account, account_id) -> None` (raises `403` unless operator or own account).
  - `KeyOut` model: `id: int`, `name: str`, `active: bool`, `created_at: datetime`.
  - `KeyCreateRequest` model: `name: str`.
  - `KeyCreatedResponse` model: `id`, `name`, `active`, `created_at`, `key: str` (raw, once).
  - Routes: `GET /accounts/{account_id}/keys`, `POST /accounts/{account_id}/keys`, `POST /accounts/{account_id}/keys/{key_id}/revoke`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
async def test_list_own_keys(client, raw_key, session):
    """An account can list its own keys via its own account id."""
    me = (await client.get("/dashboard/api/me", headers={"Authorization": f"Bearer {raw_key}"})).json()
    resp = await client.get(
        f"/dashboard/api/accounts/{me['account_id']}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    names = [k["name"] for k in resp.json()["keys"]]
    assert "dashboard-test" in names


async def test_mint_key_returns_raw_once(client, raw_key):
    """Minting a key returns the raw key exactly once in the response body."""
    me = (await client.get("/dashboard/api/me", headers={"Authorization": f"Bearer {raw_key}"})).json()
    resp = await client.post(
        f"/dashboard/api/accounts/{me['account_id']}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"name": "minted"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"].startswith("gk-")
    assert body["name"] == "minted"
    assert body["active"] is True


async def test_mint_duplicate_name_conflicts(client, raw_key):
    """A duplicate key name maps to 409."""
    me = (await client.get("/dashboard/api/me", headers={"Authorization": f"Bearer {raw_key}"})).json()
    aid = me["account_id"]
    await client.post(
        f"/dashboard/api/accounts/{aid}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"name": "dup"},
    )
    resp = await client.post(
        f"/dashboard/api/accounts/{aid}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"name": "dup"},
    )
    assert resp.status_code == 409


async def test_revoke_flips_active(client, raw_key):
    """Revoking a key sets active False; it stays listed."""
    me = (await client.get("/dashboard/api/me", headers={"Authorization": f"Bearer {raw_key}"})).json()
    aid = me["account_id"]
    minted = (
        await client.post(
            f"/dashboard/api/accounts/{aid}/keys",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"name": "to-revoke"},
        )
    ).json()
    resp = await client.post(
        f"/dashboard/api/accounts/{aid}/keys/{minted['id']}/revoke",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is False


async def test_non_operator_cannot_touch_other_account_keys(client, raw_key, session):
    """A non-operator listing another account's keys is 403."""
    other = await create_account(session, name="other-acct")
    await session.commit()
    resp = await client.get(
        f"/dashboard/api/accounts/{other.id}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 403


async def test_operator_can_list_any_account_keys(client, operator_key, session):
    """An operator can list another account's keys."""
    other = await create_account(session, name="tenant-x")
    await session.commit()
    resp = await client.get(
        f"/dashboard/api/accounts/{other.id}/keys",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "keys or mint or revoke or operator_can_list" -v`
Expected: FAIL - `404` on the list route (not defined).

- [ ] **Step 3: Add the authz helper, models, and routes**

In `gatekeep/api/dashboard.py`, add the shared authz helper after `require_operator`:

```python
def _authorize_account_access(caller_account: Account, account_id: int) -> None:
    """Authorize an account-scoped action: operator, or the caller's own account.

    Raises:
        HTTPException: 403 when a non-operator targets a different account.
    """
    if caller_account.is_operator or caller_account.id == account_id:
        return
    raise _forbidden("You can only manage your own account.")
```

Add the models and routes:

```python
class KeyOut(BaseModel):
    """One API key as shown in the management UI (no secret material)."""

    id: int
    name: str
    active: bool
    created_at: datetime


class KeyListResponse(BaseModel):
    """An account's keys, active and revoked, newest first."""

    keys: list[KeyOut]


class KeyCreateRequest(BaseModel):
    """Request body for minting a key: the new key's display name."""

    name: str


class KeyCreatedResponse(BaseModel):
    """A freshly minted key. `key` carries the raw secret exactly once."""

    id: int
    name: str
    active: bool
    created_at: datetime
    key: str


@router.get("/accounts/{account_id}/keys", response_model=KeyListResponse)
async def list_account_keys(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> KeyListResponse:
    """List an account's keys. Allowed for the account itself or an operator.

    Raises 403 for a non-operator targeting another account, 404 for an
    unknown account.
    """
    _authorize_account_access(caller_account, account_id)
    try:
        keys = await account_service.list_keys(session, account_id)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    return KeyListResponse(
        keys=[
            KeyOut(id=k.id, name=k.name, active=k.active, created_at=k.created_at)
            for k in keys
        ]
    )


@router.post("/accounts/{account_id}/keys", response_model=KeyCreatedResponse)
async def mint_account_key(
    account_id: int,
    body: KeyCreateRequest,
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> KeyCreatedResponse:
    """Mint a key for an account, returning the raw key exactly once.

    Raises 403 (wrong account), 404 (unknown account), 409 (duplicate name).
    """
    _authorize_account_access(caller_account, account_id)
    try:
        key, raw = await account_service.create_key(session, account_id, body.name)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except account_service.KeyNameConflictError as exc:
        raise HTTPException(status_code=409, detail=_error_body(str(exc))) from exc
    return KeyCreatedResponse(
        id=key.id, name=key.name, active=key.active, created_at=key.created_at, key=raw
    )


@router.post("/accounts/{account_id}/keys/{key_id}/revoke", response_model=KeyOut)
async def revoke_account_key(
    account_id: int,
    key_id: int,
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> KeyOut:
    """Soft-revoke a key on an account. Raises 403 (wrong account) or 404
    (no such key on the account).
    """
    _authorize_account_access(caller_account, account_id)
    try:
        key = await account_service.revoke_key(session, account_id, key_id)
    except account_service.KeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    return KeyOut(id=key.id, name=key.name, active=key.active, created_at=key.created_at)
```

Add the shared error-body helper once, near `_forbidden`:

```python
def _error_body(message: str, err_type: str = "invalid_request_error") -> dict:
    """Build an OpenAI-shaped error detail dict for HTTPException(detail=...)."""
    return {"error": {"message": message, "type": err_type, "code": None}}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard.py -k "keys or mint or revoke or operator_can_list" -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff format gatekeep/api/dashboard.py tests/test_dashboard.py
ruff check gatekeep/api/dashboard.py tests/test_dashboard.py
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat: add account-scoped key list/mint/revoke routes"
```

---

## Task 5: API - operator account routes (list / create / patch)

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 3's `require_operator`, `_get_redis`, `_error_body`; Task 1/2's `create_account` / `rename_account` / `set_budget` / `set_operator` / `list_accounts_with_stats` and their errors.
- Produces:
  - `AccountStatsOut` model mirroring `AccountStats` fields.
  - `AccountListResponse` model: `accounts: list[AccountStatsOut]`.
  - `AccountCreateRequest`: `name: str`, `monthly_budget_usd: float | None = None`, `is_operator: bool = False`.
  - `AccountPatchRequest`: `name: str | None = None`, `monthly_budget_usd: float | None = None`, `clear_budget: bool = False`, `is_operator: bool | None = None`.
  - `AccountOut` model: `id`, `name`, `is_operator`, `monthly_budget_usd`, `created_at`.
  - Routes: `GET /accounts` (operator), `POST /accounts` (operator), `PATCH /accounts/{account_id}` (operator).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
async def test_list_accounts_operator_only(client, raw_key):
    """A non-operator hitting GET /accounts is 403."""
    resp = await client.get(
        "/dashboard/api/accounts", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert resp.status_code == 403


async def test_list_accounts_returns_stats(client, operator_key):
    """An operator gets accounts with counts, budget, and spend fields."""
    resp = await client.get(
        "/dashboard/api/accounts", headers={"Authorization": f"Bearer {operator_key}"}
    )
    assert resp.status_code == 200
    rows = resp.json()["accounts"]
    assert rows, "expected at least the operator's own account"
    sample = rows[0]
    assert set(sample) >= {
        "id",
        "name",
        "is_operator",
        "monthly_budget_usd",
        "created_at",
        "active_key_count",
        "total_key_count",
        "spend_mtd",
    }


async def test_create_account_operator(client, operator_key):
    """An operator can create an account."""
    resp = await client.post(
        "/dashboard/api/accounts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "new-tenant", "monthly_budget_usd": 50.0},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new-tenant"


async def test_create_account_name_conflict(client, operator_key):
    """A duplicate account name maps to 409."""
    await client.post(
        "/dashboard/api/accounts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "dupe"},
    )
    resp = await client.post(
        "/dashboard/api/accounts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "dupe"},
    )
    assert resp.status_code == 409


async def test_create_account_bad_budget(client, operator_key):
    """A non-positive budget maps to 422."""
    resp = await client.post(
        "/dashboard/api/accounts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "cheapo", "monthly_budget_usd": 0},
    )
    assert resp.status_code == 422


async def test_patch_account_rename_and_budget(client, operator_key, session):
    """An operator can rename and set budget in one PATCH."""
    target = await create_account(session, name="patch-me")
    await session.commit()
    resp = await client.patch(
        f"/dashboard/api/accounts/{target.id}",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "patched", "monthly_budget_usd": 12.5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "patched"
    assert body["monthly_budget_usd"] == 12.5


async def test_patch_clear_budget(client, operator_key, session):
    """clear_budget True sets the cap to null."""
    target = await create_account(session, name="had-budget", monthly_budget_usd=9.0)
    await session.commit()
    resp = await client.patch(
        f"/dashboard/api/accounts/{target.id}",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"clear_budget": True},
    )
    assert resp.status_code == 200
    assert resp.json()["monthly_budget_usd"] is None


async def test_patch_last_operator_guard(client, operator_key, session):
    """Turning off the only operator maps to 409."""
    # operator_key's own account is the only operator; find its id via /me.
    me = (
        await client.get(
            "/dashboard/api/me", headers={"Authorization": f"Bearer {operator_key}"}
        )
    ).json()
    resp = await client.patch(
        f"/dashboard/api/accounts/{me['account_id']}",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"is_operator": False},
    )
    assert resp.status_code == 409
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k "accounts or patch or create_account" -v`
Expected: FAIL - `403`/`404`/`405` on routes that do not exist yet.

- [ ] **Step 3: Add the models and routes**

In `gatekeep/api/dashboard.py`, add the models and routes:

```python
class AccountStatsOut(BaseModel):
    """One account row for the operator's all-accounts table."""

    id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    created_at: datetime
    active_key_count: int
    total_key_count: int
    spend_mtd: float


class AccountListResponse(BaseModel):
    """All accounts with stats, ordered by name (operator view)."""

    accounts: list[AccountStatsOut]


class AccountOut(BaseModel):
    """A single account after a create/patch, without stats."""

    id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    created_at: datetime


class AccountCreateRequest(BaseModel):
    """Request body for creating an account."""

    name: str
    monthly_budget_usd: float | None = None
    is_operator: bool = False


class AccountPatchRequest(BaseModel):
    """Request body for updating an account.

    Every field is optional so a caller sends only what changes. `clear_budget`
    is a separate flag because `monthly_budget_usd = null` is indistinguishable
    from "field omitted" in JSON, and the two must mean different things
    (clear-the-cap vs. leave-it-alone).
    """

    name: str | None = None
    monthly_budget_usd: float | None = None
    clear_budget: bool = False
    is_operator: bool | None = None


def _account_out(account: Account) -> AccountOut:
    """Map an Account ORM row to the AccountOut response model."""
    return AccountOut(
        id=account.id,
        name=account.name,
        is_operator=account.is_operator,
        monthly_budget_usd=account.monthly_budget_usd,
        created_at=account.created_at,
    )


@router.get("/accounts", response_model=AccountListResponse)
async def list_accounts(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(_get_redis),
    _operator: Account = Depends(require_operator),
) -> AccountListResponse:
    """List all accounts with key counts and month-to-date spend. Operator only."""
    stats = await account_service.list_accounts_with_stats(session, redis)
    return AccountListResponse(
        accounts=[
            AccountStatsOut(
                id=s.id,
                name=s.name,
                is_operator=s.is_operator,
                monthly_budget_usd=s.monthly_budget_usd,
                created_at=s.created_at,
                active_key_count=s.active_key_count,
                total_key_count=s.total_key_count,
                spend_mtd=s.spend_mtd,
            )
            for s in stats
        ]
    )


@router.post("/accounts", response_model=AccountOut)
async def create_account_route(
    body: AccountCreateRequest,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> AccountOut:
    """Create an account. Operator only. 409 on name collision, 422 on bad budget."""
    try:
        account = await account_service.create_account(
            session,
            name=body.name,
            monthly_budget_usd=body.monthly_budget_usd,
            is_operator=body.is_operator,
        )
    except account_service.InvalidBudgetError as exc:
        raise HTTPException(status_code=422, detail=_error_body(str(exc))) from exc
    except account_service.AccountNameConflictError as exc:
        raise HTTPException(status_code=409, detail=_error_body(str(exc))) from exc
    return _account_out(account)


@router.patch("/accounts/{account_id}", response_model=AccountOut)
async def patch_account_route(
    account_id: int,
    body: AccountPatchRequest,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> AccountOut:
    """Rename, set/clear budget, and/or toggle operator on an account.

    Operator only. Applies each requested change through the service layer so
    its guards (last-operator, name uniqueness, budget validation) hold.
    Maps 404 (unknown account), 409 (name collision, last-operator), 422
    (bad budget).
    """
    try:
        account = None
        if body.name is not None:
            account = await account_service.rename_account(session, account_id, body.name)
        if body.clear_budget:
            account = await account_service.set_budget(session, account_id, None)
        elif body.monthly_budget_usd is not None:
            account = await account_service.set_budget(
                session, account_id, body.monthly_budget_usd
            )
        if body.is_operator is not None:
            account = await account_service.set_operator(
                session, account_id, body.is_operator
            )
        if account is None:
            # No mutating field supplied; return the current state.
            account = await account_service._get_account_or_404(session, account_id)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except account_service.InvalidBudgetError as exc:
        raise HTTPException(status_code=422, detail=_error_body(str(exc))) from exc
    except (
        account_service.AccountNameConflictError,
        account_service.LastOperatorError,
    ) as exc:
        raise HTTPException(status_code=409, detail=_error_body(str(exc))) from exc
    return _account_out(account)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard.py -k "accounts or patch or create_account" -v`
Expected: PASS.

- [ ] **Step 5: Run the whole dashboard + service suite and commit**

```bash
pytest tests/test_dashboard.py tests/test_account_service.py -v
ruff format gatekeep/api/dashboard.py tests/test_dashboard.py
ruff check gatekeep/api/dashboard.py tests/test_dashboard.py
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat: add operator account list/create/patch routes"
```

---

## Task 6: CLI - account and key subcommand groups

**Files:**
- Modify: `gatekeep/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1/2 service functions and errors.
- Produces (CLI handlers, all `async`):
  - `_account_create(name, budget, unlimited, operator)`, `_account_rename(name, new_name)`, `_account_set_budget(name, amount, unlimited)`, `_account_set_operator(name, off)`, `_account_list()`.
  - `_key_create(account, name)`, `_key_revoke(account, name)`, `_key_list(account)`.
  - `_resolve_account_id(session, name) -> int` helper.
  - `key set-budget` removed; `account set-budget` is the only budget command.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (extend the existing imports from `gatekeep.cli` to include the new handlers and `_resolve_account_id`):

```python
from gatekeep.cli import (
    _account_create,
    _account_list,
    _account_set_operator,
    _key_create,
    _key_list,
    _resolve_account_id,
)
from gatekeep import account_service
from gatekeep.models import ApiKey
from sqlalchemy import select


async def test_account_create_and_resolve(session, capsys):
    """`account create` makes an account resolvable by name."""
    await _account_create("team-a", budget=None, unlimited=False, operator=False)
    account_id = await _resolve_account_id(session, "team-a")
    assert account_id is not None


async def test_account_set_operator_bootstrap(session):
    """`account set-operator` promotes an account (the headless bootstrap path)."""
    await _account_create("boot", budget=None, unlimited=False, operator=False)
    await _account_set_operator("boot", off=False)
    account_id = await _resolve_account_id(session, "boot")
    from gatekeep.models import Account

    assert (await session.get(Account, account_id)).is_operator is True


async def test_key_create_prints_raw_and_persists(session, capsys):
    """`key create` mints a key, prints the raw value, and stores its hash."""
    await _account_create("team-k", budget=None, unlimited=False, operator=False)
    await _key_create("team-k", "prod")
    printed = capsys.readouterr().out
    assert "gk-" in printed
    account_id = await _resolve_account_id(session, "team-k")
    keys = (
        await session.execute(select(ApiKey).where(ApiKey.account_id == account_id))
    ).scalars().all()
    assert [k.name for k in keys] == ["prod"]


def test_key_set_budget_removed():
    """`key set-budget` no longer parses; budget moved to `account set-budget`."""
    from gatekeep.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["key", "set-budget", "team-k", "10"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_cli.py -k "account or key_create or key_set_budget_removed" -v`
Expected: FAIL - `ImportError` for the new handler names.

- [ ] **Step 3: Rewrite the CLI account/key handling**

In `gatekeep/cli.py`, add the service import:

```python
from gatekeep import account_service
```

Add a resolver and the new handlers (place near the other command handlers):

```python
async def _resolve_account_id(session, name: str) -> int:
    """Return the id of the account named `name`, or raise ValueError.

    Central lookup so every account/key subcommand references accounts by
    their human-facing name, matching how operators think about them.
    """
    account = (
        await session.execute(select(Account).where(Account.name == name))
    ).scalar_one_or_none()
    if account is None:
        raise ValueError(f"no account named {name!r}")
    return account.id


async def _account_create(
    name: str, budget: float | None, unlimited: bool, operator: bool
) -> None:
    """Create an account, optionally with a budget cap and operator status.

    `--unlimited` and a positive `budget` are mutually exclusive ways to set
    the cap; omitting both leaves the account unlimited.
    """
    monthly = None if unlimited else budget
    async with SessionLocal() as session:
        account = await account_service.create_account(
            session, name=name, monthly_budget_usd=monthly, is_operator=operator
        )
    flag = " (operator)" if account.is_operator else ""
    print(f"created account {name!r}{flag}")


async def _account_rename(name: str, new_name: str) -> None:
    """Rename an account."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, name)
        await account_service.rename_account(session, account_id, new_name)
    print(f"renamed {name!r} to {new_name!r}")


async def _account_set_budget(name: str, amount: float | None, unlimited: bool) -> None:
    """Set or clear an account's monthly USD spend cap, looked up by name."""
    if not unlimited and amount is None:
        raise ValueError("must provide an amount, or pass --unlimited to clear it")
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, name)
        await account_service.set_budget(session, account_id, None if unlimited else amount)
    if unlimited:
        print(f"cleared budget cap for {name!r} (unlimited)")
    else:
        print(f"set budget cap for {name!r} to ${amount:.2f}/month")


async def _account_set_operator(name: str, off: bool) -> None:
    """Grant or revoke operator status for an account (guarded server-side)."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, name)
        await account_service.set_operator(session, account_id, not off)
    print(f"{'revoked' if off else 'granted'} operator for {name!r}")


async def _account_list() -> None:
    """Print every account with its budget and operator flag."""
    async with SessionLocal() as session:
        accounts = (
            (await session.execute(select(Account).order_by(Account.name))).scalars().all()
        )
        for account in accounts:
            budget = (
                "unlimited"
                if account.monthly_budget_usd is None
                else f"${account.monthly_budget_usd:.2f}"
            )
            flag = "\toperator" if account.is_operator else ""
            print(f"{account.name}\t{budget}{flag}")


async def _key_create(account: str, name: str) -> None:
    """Mint a key for an account and print the raw key exactly once."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, account)
        _key, raw = await account_service.create_key(session, account_id, name)
    print(raw)


async def _key_revoke(account: str, name: str) -> None:
    """Soft-revoke a key by account name and key name."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, account)
        key = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.account_id == account_id, ApiKey.name == name
                )
            )
        ).scalar_one_or_none()
        if key is None:
            raise ValueError(f"no key named {name!r} on account {account!r}")
        await account_service.revoke_key(session, account_id, key.id)
    print(f"revoked key {name!r} on account {account!r}")


async def _key_list(account: str) -> None:
    """Print an account's keys (active and revoked)."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, account)
        keys = await account_service.list_keys(session, account_id)
        for key in keys:
            status = "active" if key.active else "revoked"
            print(f"{key.name}\t{status}")
```

Add `ApiKey` to the models import in `cli.py`:

```python
from gatekeep.models import Account, ApiKey, PromptVersion
```

Register `AccountServiceError` handling and remove `key set-budget`. In `build_parser`, replace the `key` subparser block (the one that defined `set-budget`) with:

```python
    account_parser = subparsers.add_parser("account", help="manage accounts (tenants)")
    account_subparsers = account_parser.add_subparsers(dest="account_command", required=True)

    ac_create = account_subparsers.add_parser("create", help="create an account")
    ac_create.add_argument("name")
    ac_create.add_argument("--budget", type=float, default=None, help="monthly cap in USD")
    ac_create.add_argument(
        "--unlimited", action="store_true", help="no budget cap (default)"
    )
    ac_create.add_argument(
        "--operator", action="store_true", help="grant operator status"
    )

    ac_rename = account_subparsers.add_parser("rename", help="rename an account")
    ac_rename.add_argument("name")
    ac_rename.add_argument("new_name")

    ac_budget = account_subparsers.add_parser(
        "set-budget", help="set or clear an account's monthly USD spend cap"
    )
    ac_budget.add_argument("name", help="the account name")
    ac_budget.add_argument(
        "amount", type=float, nargs="?", default=None, help="new monthly cap in USD"
    )
    ac_budget.add_argument(
        "--unlimited", action="store_true", help="clear the cap (unlimited spend)"
    )

    ac_operator = account_subparsers.add_parser(
        "set-operator", help="grant (default) or revoke operator status"
    )
    ac_operator.add_argument("name")
    ac_operator.add_argument(
        "--off", action="store_true", help="revoke operator status instead of granting"
    )

    account_subparsers.add_parser("list", help="list all accounts")

    key_parser = subparsers.add_parser("key", help="manage API keys")
    key_subparsers = key_parser.add_subparsers(dest="key_command", required=True)

    k_create = key_subparsers.add_parser("create", help="mint a key for an account")
    k_create.add_argument("account", help="the account name")
    k_create.add_argument("name", help="the new key's name")

    k_revoke = key_subparsers.add_parser("revoke", help="soft-revoke a key")
    k_revoke.add_argument("account", help="the account name")
    k_revoke.add_argument("name", help="the key's name")

    k_list = key_subparsers.add_parser("list", help="list an account's keys")
    k_list.add_argument("account", help="the account name")
```

In `main()`, replace the `key set-budget` dispatch block and add the `account` dispatch. Replace:

```python
        elif args.command == "key":
            if args.key_command == "set-budget":
                asyncio.run(_set_budget(args.name, args.amount, args.unlimited))
```

with:

```python
        elif args.command == "account":
            if args.account_command == "create":
                asyncio.run(
                    _account_create(args.name, args.budget, args.unlimited, args.operator)
                )
            elif args.account_command == "rename":
                asyncio.run(_account_rename(args.name, args.new_name))
            elif args.account_command == "set-budget":
                asyncio.run(_account_set_budget(args.name, args.amount, args.unlimited))
            elif args.account_command == "set-operator":
                asyncio.run(_account_set_operator(args.name, args.off))
            elif args.account_command == "list":
                asyncio.run(_account_list())
        elif args.command == "key":
            if args.key_command == "create":
                asyncio.run(_key_create(args.account, args.name))
            elif args.key_command == "revoke":
                asyncio.run(_key_revoke(args.account, args.name))
            elif args.key_command == "list":
                asyncio.run(_key_list(args.account))
```

Add `AccountServiceError` to the `except` clause in `main()` so service errors print cleanly. Change:

```python
    except (PromptNotFoundError, PromptVersionNotFoundError, ValueError) as exc:
```

to:

```python
    except (
        PromptNotFoundError,
        PromptVersionNotFoundError,
        account_service.AccountServiceError,
        ValueError,
    ) as exc:
```

Delete the now-unused `_set_budget` function from `cli.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (existing CLI tests plus the new account/key tests; the old `test_*_set_budget` tests that imported `_set_budget` must be updated to the new `_account_set_budget` name - update any such references in `tests/test_cli.py`).

- [ ] **Step 5: Lint and commit**

```bash
ruff format gatekeep/cli.py tests/test_cli.py
ruff check gatekeep/cli.py tests/test_cli.py
git add gatekeep/cli.py tests/test_cli.py
git commit -m "feat: add account and key CLI groups; move set-budget under account"
```

---

## Task 7: Scripts - fix create_key.py and init-test-key.sh

**Files:**
- Modify: `scripts/create_key.py`
- Modify: `scripts/init-test-key.sh`

**Interfaces:**
- Consumes: Task 1/2's `create_account` / `create_key`; `AccountNameConflictError`.

- [ ] **Step 1: Rewrite `scripts/create_key.py` to route through the service layer**

Replace the entire contents of `scripts/create_key.py`:

```python
"""Mint an API key for an account and print the raw key exactly once.

Creates the account if it does not exist yet. Fixes the previous breakage
where an ApiKey was inserted with no account_id (non-nullable since the
accounts migration).

Usage: python scripts/create_key.py <account-name> [key-name]
"""

import asyncio
import sys

from sqlalchemy import select

from gatekeep import account_service
from gatekeep.db import SessionLocal
from gatekeep.models import Account


async def main(account_name: str, key_name: str) -> None:
    """Ensure `account_name` exists, mint `key_name` on it, and print the raw key."""
    async with SessionLocal() as session:
        account = (
            await session.execute(select(Account).where(Account.name == account_name))
        ).scalar_one_or_none()
        if account is None:
            account = await account_service.create_account(session, name=account_name)
        _key, raw = await account_service.create_key(session, account.id, key_name)
    print(raw)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(
            "usage: python scripts/create_key.py <account-name> [key-name]",
            file=sys.stderr,
        )
        raise SystemExit(1)
    account_arg = sys.argv[1]
    key_arg = sys.argv[2] if len(sys.argv) == 3 else "default"
    asyncio.run(main(account_arg, key_arg))
```

- [ ] **Step 2: Verify the script runs end to end**

Run (Postgres + Redis + migrations available):

```bash
python scripts/create_key.py smoke-acct smoke-key
```

Expected: prints a single `gk-...` line and exits 0. Re-running with a new key name mints another key on the same account; re-running with the same key name prints a "key name ... is already used" error and exits non-zero (unhandled `KeyNameConflictError` traceback is acceptable for this dev script, or wrap in try/except to print cleanly - keep it simple).

- [ ] **Step 3: Rewrite `scripts/init-test-key.sh` to mint via the service layer**

Replace the body of `scripts/init-test-key.sh` so it no longer builds an `INSERT` by hand and no longer requires host `psql`. It mints through `create_key.py` (which runs migrations-agnostic, through the ORM), and derives the raw key from that script's stdout:

```bash
#!/bin/bash
set -e

# Helper script to mint a test API key through the service layer and print a
# ready-to-use curl example.
# Usage: bash scripts/init-test-key.sh [account-name] [key-name]
#
# Examples:
#   bash scripts/init-test-key.sh                 # account 'test-account', key 'test-key'
#   bash scripts/init-test-key.sh my-acct my-key

ACCOUNT_NAME="${1:-test-account}"
KEY_NAME="${2:-test-key}"

if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 not found"
    exit 1
fi

# Ensure gatekeep is importable (alembic env.py and the ORM both need it).
echo "📦 Checking if gatekeep package is installed..."
if ! python3 -c "import gatekeep" 2>/dev/null; then
    echo "⚠️  gatekeep package not installed. Installing in editable mode..."
    if ! pip install -e . >/dev/null 2>&1; then
        echo "❌ Error: failed to install gatekeep package"
        exit 1
    fi
fi

# Bring the schema up to date (no-op if already at head).
echo "🔍 Applying migrations (if any)..."
alembic upgrade head

echo "🔑 Minting API key for account '$ACCOUNT_NAME'..."
RAW_KEY=$(python3 scripts/create_key.py "$ACCOUNT_NAME" "$KEY_NAME")

# Get default model from environment, .env file, or fallback
if [ -z "$DEFAULT_MODEL" ] && [ -f .env ]; then
    DEFAULT_MODEL=$(grep "^DEFAULT_MODEL=" .env | cut -d= -f2)
fi
DEFAULT_MODEL="${DEFAULT_MODEL:-claude-sonnet-5}"

echo ""
echo "✅ Test API key created successfully!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Account:  $ACCOUNT_NAME"
echo "Key Name: $KEY_NAME"
echo "Raw Key:  $RAW_KEY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "💡 Use this in curl requests:"
echo ""
echo "curl -X POST http://localhost:8100/v1/chat/completions \\"
echo "  -H \"Authorization: Bearer $RAW_KEY\" \\"
echo "  -H \"Content-Type: application/json\" \\"
echo "  -d '{\"model\": \"$DEFAULT_MODEL\", \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}'"
echo ""
```

- [ ] **Step 4: Verify the shell script**

Run:

```bash
bash scripts/init-test-key.sh init-smoke init-smoke-key
```

Expected: prints the banner with a `gk-...` raw key and a curl example; exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/create_key.py scripts/init-test-key.sh
git commit -m "fix: route create_key.py and init-test-key.sh through the account service"
```

---

## Task 8: Frontend - API types and client helpers

> Frontend has no test runner configured; these tasks are verified by `npm run build` (type-check) plus manual UI checks. Build from `dashboard/`.

**Files:**
- Modify: `dashboard/src/api/types.ts`
- Modify: `dashboard/src/api/client.ts`

**Interfaces:**
- Produces: TS types `MeResponse`, `KeyOut`, `KeyListResponse`, `KeyCreatedResponse`, `AccountStatsOut`, `AccountListResponse`, `AccountOut`; and client functions `getMe`, `getAccountKeys`, `createKey`, `revokeKey`, `getAccounts`, `createAccount`, `patchAccount`.

- [ ] **Step 1: Add the types**

Append to `dashboard/src/api/types.ts`:

```typescript
/** The caller's own account context, from GET /me. Drives tab visibility
 * and the budget card. */
export interface MeResponse {
  account_id: number;
  name: string;
  is_operator: boolean;
  monthly_budget_usd: number | null;
  spend_mtd: number;
}

/** One API key as shown in the management UI (no secret material). */
export interface KeyOut {
  id: number;
  name: string;
  active: boolean;
  created_at: string;
}

/** An account's keys, active and revoked, newest first. */
export interface KeyListResponse {
  keys: KeyOut[];
}

/** A freshly minted key; `key` carries the raw secret exactly once. */
export interface KeyCreatedResponse {
  id: number;
  name: string;
  active: boolean;
  created_at: string;
  key: string;
}

/** One account row for the operator's all-accounts table. */
export interface AccountStatsOut {
  id: number;
  name: string;
  is_operator: boolean;
  monthly_budget_usd: number | null;
  created_at: string;
  active_key_count: number;
  total_key_count: number;
  spend_mtd: number;
}

/** All accounts with stats, ordered by name (operator view). */
export interface AccountListResponse {
  accounts: AccountStatsOut[];
}

/** A single account after create/patch, without stats. */
export interface AccountOut {
  id: number;
  name: string;
  is_operator: boolean;
  monthly_budget_usd: number | null;
  created_at: string;
}

/** Request body for creating an account. */
export interface AccountCreateRequest {
  name: string;
  monthly_budget_usd?: number | null;
  is_operator?: boolean;
}

/** Request body for updating an account; only supplied fields change.
 * `clear_budget` clears the cap (distinct from omitting the field). */
export interface AccountPatchRequest {
  name?: string;
  monthly_budget_usd?: number | null;
  clear_budget?: boolean;
  is_operator?: boolean;
}
```

- [ ] **Step 2: Add a shared mutating-request wrapper and helpers**

In `dashboard/src/api/client.ts`, add the new type imports to the existing import block:

```typescript
import type {
  AccountCreateRequest,
  AccountListResponse,
  AccountOut,
  AccountPatchRequest,
  KeyCreatedResponse,
  KeyListResponse,
  MeResponse,
  // ...existing imports stay...
} from "./types";
```

Add a `mutate` helper next to `request` (it mirrors `request`'s auth + 401 handling but sends a JSON body via POST/PATCH):

```typescript
/**
 * Issues an authenticated POST/PATCH against `/dashboard/api/<path>` with a
 * JSON body, mirroring `request`'s bearer-auth and 401 handling.
 *
 * @param method - "POST" or "PATCH".
 * @param path - API path under `/dashboard/api/`, without a leading slash.
 * @param body - JSON-serializable request body, or undefined for none.
 * @returns The parsed JSON response body.
 * @throws {UnauthorizedError} If no key is stored, or the gateway responds 401.
 * @throws {Error} For any other non-OK response; the thrown message includes
 *   the server's error message when the body is OpenAI-shaped.
 */
async function mutate<T>(
  method: "POST" | "PATCH",
  path: string,
  body?: unknown,
): Promise<T> {
  const apiKey = getStoredApiKey();
  if (!apiKey) {
    throw new UnauthorizedError("No API key stored");
  }
  const url = new URL(`/dashboard/api/${path}`, window.location.origin);
  const response = await fetch(url.toString(), {
    method,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    clearStoredApiKey();
    throw new UnauthorizedError("API key was rejected");
  }
  if (!response.ok) {
    let message = `Request to ${path} failed with status ${response.status}`;
    try {
      const payload = await response.json();
      if (payload?.error?.message) message = payload.error.message;
    } catch {
      // Non-JSON error body; keep the generic message.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

/** Fetches the caller's own account context (id, name, operator flag,
 * budget, spend). */
export function getMe(): Promise<MeResponse> {
  return request<MeResponse>("me");
}

/** Lists an account's keys (active and revoked). */
export function getAccountKeys(accountId: number): Promise<KeyListResponse> {
  return request<KeyListResponse>(`accounts/${accountId}/keys`);
}

/** Mints a key for an account; the response carries the raw key once. */
export function createKey(accountId: number, name: string): Promise<KeyCreatedResponse> {
  return mutate<KeyCreatedResponse>("POST", `accounts/${accountId}/keys`, { name });
}

/** Soft-revokes a key on an account. */
export function revokeKey(accountId: number, keyId: number): Promise<KeyOut> {
  return mutate<KeyOut>("POST", `accounts/${accountId}/keys/${keyId}/revoke`);
}

/** Lists all accounts with stats (operator only). */
export function getAccounts(): Promise<AccountListResponse> {
  return request<AccountListResponse>("accounts");
}

/** Creates an account (operator only). */
export function createAccount(body: AccountCreateRequest): Promise<AccountOut> {
  return mutate<AccountOut>("POST", "accounts", body);
}

/** Updates an account (operator only). */
export function patchAccount(
  accountId: number,
  body: AccountPatchRequest,
): Promise<AccountOut> {
  return mutate<AccountOut>("PATCH", `accounts/${accountId}`, body);
}
```

Add `KeyOut` to the type import block (it is referenced by `revokeKey`'s return type).

- [ ] **Step 3: Type-check**

Run: `cd dashboard && npm run build`
Expected: build succeeds with no TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/api/types.ts dashboard/src/api/client.ts
git commit -m "feat: add management API types and POST/PATCH client helpers"
```

---

## Task 9: Frontend - tab control, ManagementPage, self-service (budget card, key table, mint modal)

**Files:**
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/src/components/Header.tsx`
- Modify: `dashboard/src/pages/DashboardPage.tsx`
- Create: `dashboard/src/pages/ManagementPage.tsx`
- Create: `dashboard/src/components/BudgetCard.tsx`
- Create: `dashboard/src/components/KeyTable.tsx`
- Create: `dashboard/src/components/CreateKeyModal.tsx`

**Interfaces:**
- Consumes: Task 8 client helpers and types; `formatUsd` from `format.ts`.
- Produces: a `TabKey = "analytics" | "management"` shared through `App`; `ManagementPage` composing `BudgetCard`, `KeyTable`, `CreateKeyModal`, and (Task 10) the operator section.

- [ ] **Step 1: Add the tab control to the header**

Replace `dashboard/src/components/Header.tsx`:

```typescript
export type TabKey = "analytics" | "management";

interface HeaderProps {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  onClearKey: () => void;
}

/** Dashboard top bar: app title, an Analytics / Accounts & Keys tab control,
 * and a button to clear/replace the stored API key. */
export default function Header({ activeTab, onTabChange, onClearKey }: HeaderProps) {
  const tabClass = (tab: TabKey) =>
    `rounded px-3 py-1.5 text-sm ${
      activeTab === tab
        ? "bg-slate-800 text-slate-100"
        : "text-slate-400 hover:text-slate-200"
    }`;

  return (
    <header className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
      <div className="flex items-center gap-4">
        <span className="text-lg font-semibold tracking-tight text-slate-100">Gatekeep</span>
        <nav className="flex items-center gap-1">
          <button className={tabClass("analytics")} onClick={() => onTabChange("analytics")}>
            Analytics
          </button>
          <button className={tabClass("management")} onClick={() => onTabChange("management")}>
            Accounts &amp; Keys
          </button>
        </nav>
      </div>
      <button
        onClick={onClearKey}
        title="Replace or clear stored API key"
        className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
      >
        API key
      </button>
    </header>
  );
}
```

- [ ] **Step 2: Lift the tab + caller identity into `App.tsx`**

Replace `dashboard/src/App.tsx`:

```typescript
import { useEffect, useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
import Header, { type TabKey } from "./components/Header";
import DashboardPage from "./pages/DashboardPage";
import ManagementPage from "./pages/ManagementPage";
import { UnauthorizedError, clearStoredApiKey, getMe, getStoredApiKey } from "./api/client";
import type { MeResponse } from "./api/types";

/**
 * Root component: gates the dashboard behind an API key, owns the active tab
 * and the caller's own account context (GET /me), and renders the shared
 * header plus the active tab's page.
 */
export default function App() {
  const [hasKey, setHasKey] = useState<boolean>(() => getStoredApiKey() !== null);
  const [tab, setTab] = useState<TabKey>("analytics");
  const [me, setMe] = useState<MeResponse | null>(null);

  /** Clears the stored API key and returns to the key entry screen. */
  function handleUnauthorized() {
    clearStoredApiKey();
    setMe(null);
    setHasKey(false);
  }

  useEffect(() => {
    if (!hasKey) return;
    getMe()
      .then(setMe)
      .catch((err) => {
        if (err instanceof UnauthorizedError) handleUnauthorized();
      });
  }, [hasKey]);

  if (!hasKey) {
    return <KeyEntryScreen onKeySaved={() => setHasKey(true)} />;
  }

  return (
    <div className="min-h-screen bg-slate-950">
      <Header activeTab={tab} onTabChange={setTab} onClearKey={handleUnauthorized} />
      {tab === "analytics" ? (
        <DashboardPage onUnauthorized={handleUnauthorized} />
      ) : (
        <ManagementPage me={me} onUnauthorized={handleUnauthorized} onMeChanged={setMe} />
      )}
    </div>
  );
}
```

- [ ] **Step 3: Drop the now-duplicated header/root wrapper from `DashboardPage`**

In `dashboard/src/pages/DashboardPage.tsx`, remove the `import Header from "../components/Header";` line, remove the `<Header .../>` element, and change the outer wrapper so it no longer repaints the page background (the wrapper now lives in `App`). Replace the opening `return (` block's root element:

```typescript
  return (
    <div>
      <FilterBar filters={filters} availableModels={allModels} onChange={setFilters} />
```

and close with the matching `</div>` (unchanged). Leave the rest of the panels intact.

- [ ] **Step 4: Create `BudgetCard`**

Create `dashboard/src/components/BudgetCard.tsx`:

```typescript
import { formatUsd } from "../format";
import type { MeResponse } from "../api/types";

interface BudgetCardProps {
  me: MeResponse | null;
}

/** View-only card showing the caller's monthly budget cap and live
 * month-to-date budget-relevant spend (from GET /me). Tenants cannot change
 * their own cap, so there is no edit control here. */
export default function BudgetCard({ me }: BudgetCardProps) {
  if (!me) return null;
  const cap = me.monthly_budget_usd;
  const pct = cap && cap > 0 ? Math.min(100, (me.spend_mtd / cap) * 100) : null;

  return (
    <section className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Budget (this month)</h2>
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold text-slate-100">{formatUsd(me.spend_mtd)}</span>
        <span className="text-sm text-slate-400">
          {cap === null ? "of unlimited" : `of ${formatUsd(cap)}`}
        </span>
      </div>
      {pct !== null && (
        <div className="mt-3 h-2 w-full overflow-hidden rounded bg-slate-800">
          <div
            className={pct >= 100 ? "h-full bg-red-500" : "h-full bg-indigo-500"}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      <p className="mt-2 text-xs text-slate-500">
        Budget-relevant spend (provider cost, cache hits excluded).
      </p>
    </section>
  );
}
```

- [ ] **Step 5: Create `CreateKeyModal`**

Create `dashboard/src/components/CreateKeyModal.tsx`:

```typescript
import { useState } from "react";
import { createKey } from "../api/client";

interface CreateKeyModalProps {
  accountId: number;
  onClose: () => void;
  onCreated: () => void;
}

/**
 * Two-step key mint flow:
 *  1. Name the key.
 *  2. Show the raw key exactly once with a copy button and a can't-undo
 *     warning, gated behind an "I've saved it" checkbox before it can close.
 */
export default function CreateKeyModal({ accountId, onClose, onCreated }: CreateKeyModalProps) {
  const [name, setName] = useState("");
  const [rawKey, setRawKey] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Submits step 1: mints the key and advances to the show-once panel. */
  async function handleCreate() {
    setError(null);
    setBusy(true);
    try {
      const created = await createKey(accountId, name.trim());
      setRawKey(created.key);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create key");
    } finally {
      setBusy(false);
    }
  }

  /** Closes the show-once panel and notifies the parent to refresh. */
  function handleDone() {
    onCreated();
    onClose();
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 p-6">
        {rawKey === null ? (
          <>
            <h2 className="mb-3 text-base font-semibold text-slate-100">Create API key</h2>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="key name (e.g. prod)"
              className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
            />
            {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
            <div className="flex justify-end gap-2">
              <button
                onClick={onClose}
                className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={busy || name.trim() === ""}
                className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                Create
              </button>
            </div>
          </>
        ) : (
          <>
            <h2 className="mb-2 text-base font-semibold text-slate-100">Save your API key</h2>
            <p className="mb-3 text-sm text-amber-400">
              This is shown once and cannot be recovered. Copy it now.
            </p>
            <div className="mb-3 flex items-center gap-2">
              <code className="flex-1 overflow-x-auto rounded border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100">
                {rawKey}
              </code>
              <button
                onClick={() => navigator.clipboard.writeText(rawKey)}
                className="rounded border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800"
              >
                Copy
              </button>
            </div>
            <label className="mb-4 flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              I&apos;ve saved it somewhere safe
            </label>
            <div className="flex justify-end">
              <button
                onClick={handleDone}
                disabled={!confirmed}
                className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                Done
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Create `KeyTable`**

Create `dashboard/src/components/KeyTable.tsx`:

```typescript
import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getAccountKeys, revokeKey } from "../api/client";
import type { KeyOut } from "../api/types";
import CreateKeyModal from "./CreateKeyModal";

interface KeyTableProps {
  accountId: number;
  onUnauthorized: () => void;
}

/** Lists an account's keys with a create button and per-row revoke. Revoked
 * keys stay listed, greyed out. Works for the caller's own account or, for an
 * operator, any account (the caller id is supplied by the parent). */
export default function KeyTable({ accountId, onUnauthorized }: KeyTableProps) {
  const [keys, setKeys] = useState<KeyOut[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getAccountKeys(accountId);
      setKeys(res.keys);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load keys");
    }
  }, [accountId, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  /** Revokes a key then reloads the table. */
  async function handleRevoke(keyId: number) {
    try {
      await revokeKey(accountId, keyId);
      await load();
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to revoke key");
    }
  }

  return (
    <section className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">API keys</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500"
        >
          Create key
        </button>
      </div>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            <th className="py-1">Name</th>
            <th className="py-1">Status</th>
            <th className="py-1">Created</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} className={k.active ? "text-slate-200" : "text-slate-600"}>
              <td className="py-1">{k.name}</td>
              <td className="py-1">{k.active ? "active" : "revoked"}</td>
              <td className="py-1">{new Date(k.created_at).toLocaleDateString()}</td>
              <td className="py-1 text-right">
                {k.active && (
                  <button
                    onClick={() => handleRevoke(k.id)}
                    className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
                  >
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {showCreate && (
        <CreateKeyModal
          accountId={accountId}
          onClose={() => setShowCreate(false)}
          onCreated={load}
        />
      )}
    </section>
  );
}
```

- [ ] **Step 7: Create `ManagementPage` (self-service portion; operator section is a placeholder filled in Task 10)**

Create `dashboard/src/pages/ManagementPage.tsx`:

```typescript
import BudgetCard from "../components/BudgetCard";
import KeyTable from "../components/KeyTable";
import type { MeResponse } from "../api/types";

interface ManagementPageProps {
  me: MeResponse | null;
  onUnauthorized: () => void;
  onMeChanged: (me: MeResponse) => void;
}

/**
 * Accounts & Keys tab. Every account sees its own budget card and key table;
 * operators additionally see the all-accounts section (added in a later task).
 */
export default function ManagementPage({ me, onUnauthorized }: ManagementPageProps) {
  if (!me) {
    return <p className="mx-6 mt-6 text-sm text-slate-400">Loading account...</p>;
  }
  return (
    <div className="pb-8">
      <BudgetCard me={me} />
      <KeyTable accountId={me.account_id} onUnauthorized={onUnauthorized} />
      {me.is_operator && (
        <p className="mx-6 mt-6 text-xs text-slate-600">Operator tools load below.</p>
      )}
    </div>
  );
}
```

- [ ] **Step 8: Type-check and manually verify**

Run: `cd dashboard && npm run build`
Expected: build succeeds.

Then (with the gateway running and the dashboard served) manually verify:
- The header shows two tabs; Analytics still renders every existing panel.
- Accounts & Keys shows the budget card (spend of cap) and the key table with the caller's keys.
- "Create key" opens the modal: naming then Create reveals the raw key; "Done" is disabled until the checkbox is ticked; the new key appears in the table.
- Revoke greys out a key and it stays listed.
- Be picky about pixel alignment and spacing against the existing panels; fix anything that looks off.

- [ ] **Step 9: Commit**

```bash
git add dashboard/src/App.tsx dashboard/src/components/Header.tsx dashboard/src/pages/DashboardPage.tsx dashboard/src/pages/ManagementPage.tsx dashboard/src/components/BudgetCard.tsx dashboard/src/components/KeyTable.tsx dashboard/src/components/CreateKeyModal.tsx
git commit -m "feat: add Accounts & Keys tab with budget card, key table, and mint flow"
```

---

## Task 10: Frontend - operator section (accounts table, detail panel, create-account modal)

**Files:**
- Create: `dashboard/src/components/AccountsTable.tsx`
- Create: `dashboard/src/components/AccountDetailPanel.tsx`
- Create: `dashboard/src/components/CreateAccountModal.tsx`
- Modify: `dashboard/src/pages/ManagementPage.tsx`

**Interfaces:**
- Consumes: Task 8 client helpers (`getAccounts`, `createAccount`, `patchAccount`) and `KeyTable`, `formatUsd`.
- Produces: the operator all-accounts table with a `Manage ›` action opening `AccountDetailPanel`, plus `CreateAccountModal`.

- [ ] **Step 1: Create `CreateAccountModal`**

Create `dashboard/src/components/CreateAccountModal.tsx`:

```typescript
import { useState } from "react";
import { createAccount } from "../api/client";

interface CreateAccountModalProps {
  onClose: () => void;
  onCreated: () => void;
}

/** Operator modal to create an account: name, optional budget, optional
 * operator flag. Leaving budget blank means unlimited. */
export default function CreateAccountModal({ onClose, onCreated }: CreateAccountModalProps) {
  const [name, setName] = useState("");
  const [budget, setBudget] = useState("");
  const [operator, setOperator] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Submits the create request, mapping a blank budget to unlimited (null). */
  async function handleCreate() {
    setError(null);
    setBusy(true);
    try {
      const trimmed = budget.trim();
      await createAccount({
        name: name.trim(),
        monthly_budget_usd: trimmed === "" ? null : Number(trimmed),
        is_operator: operator,
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create account");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-3 text-base font-semibold text-slate-100">Create account</h2>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="account name"
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <input
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
          placeholder="monthly budget USD (blank = unlimited)"
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <label className="mb-4 flex items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" checked={operator} onChange={(e) => setOperator(e.target.checked)} />
          Operator
        </label>
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            onClick={handleCreate}
            disabled={busy || name.trim() === ""}
            className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Create `AccountDetailPanel`**

Create `dashboard/src/components/AccountDetailPanel.tsx`:

```typescript
import { useState } from "react";
import { patchAccount } from "../api/client";
import type { AccountStatsOut } from "../api/types";
import KeyTable from "./KeyTable";

interface AccountDetailPanelProps {
  account: AccountStatsOut;
  onClose: () => void;
  onChanged: () => void;
  onUnauthorized: () => void;
}

/** Operator detail panel for one account: rename, set/clear budget, toggle
 * operator, and manage that account's keys. Each action calls PATCH (or the
 * key routes) and refreshes the parent table on success. */
export default function AccountDetailPanel({
  account,
  onClose,
  onChanged,
  onUnauthorized,
}: AccountDetailPanelProps) {
  const [name, setName] = useState(account.name);
  const [budget, setBudget] = useState(
    account.monthly_budget_usd === null ? "" : String(account.monthly_budget_usd),
  );
  const [error, setError] = useState<string | null>(null);

  /** Runs one PATCH mutation, surfaces errors, and refreshes on success. */
  async function apply(body: Parameters<typeof patchAccount>[1]) {
    setError(null);
    try {
      await patchAccount(account.id, body);
      onChanged();
    } catch (err) {
      if (err instanceof Error) setError(err.message);
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 px-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-slate-800 bg-slate-900 p-6">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-100">Manage {account.name}</h2>
          <button
            onClick={onClose}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            Close
          </button>
        </div>
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}

        <div className="mb-4 flex items-end gap-2">
          <label className="flex-1 text-xs text-slate-400">
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
            />
          </label>
          <button
            onClick={() => apply({ name: name.trim() })}
            className="rounded bg-indigo-600 px-3 py-2 text-xs text-white hover:bg-indigo-500"
          >
            Rename
          </button>
        </div>

        <div className="mb-4 flex items-end gap-2">
          <label className="flex-1 text-xs text-slate-400">
            Monthly budget USD (blank = unlimited)
            <input
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
            />
          </label>
          <button
            onClick={() =>
              apply(
                budget.trim() === ""
                  ? { clear_budget: true }
                  : { monthly_budget_usd: Number(budget.trim()) },
              )
            }
            className="rounded bg-indigo-600 px-3 py-2 text-xs text-white hover:bg-indigo-500"
          >
            Save budget
          </button>
        </div>

        <div className="mb-4 flex items-center justify-between">
          <span className="text-sm text-slate-300">
            Operator: {account.is_operator ? "yes" : "no"}
          </span>
          <button
            onClick={() => apply({ is_operator: !account.is_operator })}
            className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            {account.is_operator ? "Revoke operator" : "Make operator"}
          </button>
        </div>

        <KeyTable accountId={account.id} onUnauthorized={onUnauthorized} />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create `AccountsTable`**

Create `dashboard/src/components/AccountsTable.tsx`:

```typescript
import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getAccounts } from "../api/client";
import type { AccountStatsOut } from "../api/types";
import { formatUsd } from "../format";
import AccountDetailPanel from "./AccountDetailPanel";
import CreateAccountModal from "./CreateAccountModal";

interface AccountsTableProps {
  onUnauthorized: () => void;
}

/** Operator-only table of all accounts (name, budget, MTD spend, key count,
 * operator flag) with a Create button and a per-row Manage action. */
export default function AccountsTable({ onUnauthorized }: AccountsTableProps) {
  const [accounts, setAccounts] = useState<AccountStatsOut[]>([]);
  const [selected, setSelected] = useState<AccountStatsOut | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getAccounts();
      setAccounts(res.accounts);
      // Keep the open detail panel in sync with fresh data after a mutation.
      setSelected((cur) => (cur ? res.accounts.find((a) => a.id === cur.id) ?? null : null));
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load accounts");
    }
  }, [onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="mx-6 mt-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">All accounts</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500"
        >
          Create account
        </button>
      </div>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            <th className="py-1">Name</th>
            <th className="py-1">Budget</th>
            <th className="py-1">Spend (MTD)</th>
            <th className="py-1">Keys</th>
            <th className="py-1">Operator</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id} className="text-slate-200">
              <td className="py-1">{a.name}</td>
              <td className="py-1">
                {a.monthly_budget_usd === null ? "unlimited" : formatUsd(a.monthly_budget_usd)}
              </td>
              <td className="py-1">{formatUsd(a.spend_mtd)}</td>
              <td className="py-1">
                {a.active_key_count} active / {a.total_key_count} total
              </td>
              <td className="py-1">{a.is_operator ? "yes" : "no"}</td>
              <td className="py-1 text-right">
                <button
                  onClick={() => setSelected(a)}
                  className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
                >
                  Manage &rsaquo;
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {showCreate && (
        <CreateAccountModal onClose={() => setShowCreate(false)} onCreated={load} />
      )}
      {selected && (
        <AccountDetailPanel
          account={selected}
          onClose={() => setSelected(null)}
          onChanged={load}
          onUnauthorized={onUnauthorized}
        />
      )}
    </section>
  );
}
```

- [ ] **Step 4: Wire the operator section into `ManagementPage`**

Replace the operator placeholder in `dashboard/src/pages/ManagementPage.tsx`. Add the import:

```typescript
import AccountsTable from "../components/AccountsTable";
```

Replace the `{me.is_operator && ( ... )}` block with:

```typescript
      {me.is_operator && <AccountsTable onUnauthorized={onUnauthorized} />}
```

- [ ] **Step 5: Type-check and manually verify**

Run: `cd dashboard && npm run build`
Expected: build succeeds.

Then manually verify (as an operator key, and separately as a non-operator key):
- Operator: "All accounts" table lists every account with budget, MTD spend, "N active / M total" keys, and operator flag; "Create account" works (blank budget = unlimited); "Manage ›" opens the detail panel.
- In the detail panel: rename, set budget, clear budget (blank), toggle operator, and manage that account's keys all work; the table refreshes after each.
- Last-operator guard: revoking operator on the only operator surfaces the server's 409 message in the panel, and the flag does not change.
- Non-operator: the "All accounts" section is absent; only the budget card and own key table show.
- Pixel-check spacing/alignment of the new tables against the existing analytics panels; fix anything off.

- [ ] **Step 6: Run the full backend suite once more, then commit**

```bash
pytest tests/test_account_service.py tests/test_dashboard.py tests/test_cli.py -v
cd dashboard && npm run build && cd ..
git add dashboard/src/components/AccountsTable.tsx dashboard/src/components/AccountDetailPanel.tsx dashboard/src/components/CreateAccountModal.tsx dashboard/src/pages/ManagementPage.tsx
git commit -m "feat: add operator accounts table, detail panel, and create-account modal"
```

---

## Self-Review

**Spec coverage:**
- Authorization model (two tiers, `_account_scope`-style rule, budget operator-only) - Tasks 3-5 (`require_operator`, `_authorize_account_access`, budget only via PATCH/operator CLI).
- Guardrails (last-operator, name uniqueness 409, key uniqueness 409, budget 422, revoke scoped) - Task 1/2 service guards + Task 4/5 status mapping + tests.
- Shared service layer, `get_period_spend` (never `check_budget`) - Tasks 1-2, wired in Task 3/5.
- Month-to-date spend caveats (Redis dependency, cache-hits-excluded label) - Task 3 (`_get_redis`), BudgetCard label (Task 9), AccountStats docstring (Task 2).
- API table (7 routes, error mapping, OpenAI-shaped bodies) - Tasks 3-5.
- CLI (account + key groups, set-budget moved outright, scripts fixed) - Tasks 6-7.
- Frontend (tab control, ManagementPage, BudgetCard, KeyTable, CreateKeyModal with "I've saved it" gate, AccountsTable, AccountDetailPanel, CreateAccountModal; client POST/PATCH + types) - Tasks 8-10.
- Testing (test_account_service new, test_dashboard extended, CLI bootstrap+key-create, frontend manual) - Tasks 1-10.
- Resolved open questions #1 (move outright) and #2 (created_at + active/total counts) - Global Constraints + Task 5.

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" placeholders; every code step contains full content. The one deliberate cross-task placeholder (ManagementPage operator note in Task 9) is explicitly replaced in Task 10 Step 4.

**Type consistency:** Service names (`create_account`, `create_key -> tuple[ApiKey, str]`, `revoke_key`, `list_accounts_with_stats -> list[AccountStats]`, `get_account_spend`) are used identically in the API (Tasks 3-5) and CLI (Task 6). API response field names (`account_id`, `spend_mtd`, `active_key_count`, `total_key_count`, `key`) match the TS types (Task 8) consumed by components (Tasks 9-10). `AccountPatchRequest` (`name`/`monthly_budget_usd`/`clear_budget`/`is_operator`) matches the TS `AccountPatchRequest` and the detail panel's `apply` bodies.
