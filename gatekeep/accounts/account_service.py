from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import Integer as sa_Integer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.middleware.budget import get_period_spend, get_period_spend_batch
from gatekeep.storage.models import Account, ApiKey


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


class InvalidAccountNameError(AccountServiceError):
    """Raised when an account name is blank or all whitespace."""


def _validate_budget(amount: float | None) -> None:
    """Reject a non-positive budget; None (unlimited/cleared) is always allowed.

    Raises:
        InvalidBudgetError: if `amount` is not None and not strictly positive.
    """
    if amount is not None and amount <= 0:
        raise InvalidBudgetError("budget amount must be positive")


def _validate_name(name: str) -> None:
    """Reject a blank or whitespace-only account name.

    Raises:
        InvalidAccountNameError: if `name` is empty once stripped.
    """
    if not name.strip():
        raise InvalidAccountNameError("account name must not be blank")


async def _get_account_or_404(session: AsyncSession, account_id: int) -> Account:
    """Load an account by id or raise AccountNotFoundError."""
    account = await session.get(Account, account_id)
    if account is None:
        raise AccountNotFoundError(f"no account with id {account_id}")
    return account


async def get_account(session: AsyncSession, account_id: int) -> Account:
    """Load an account by id.

    Public accessor over `_get_account_or_404`, for callers outside this
    module (e.g. the dashboard API) that need to fetch an account without
    reaching into a private helper.

    Raises:
        AccountNotFoundError: if no account has that id.
    """
    return await _get_account_or_404(session, account_id)


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
        InvalidAccountNameError: if `name` is blank or all whitespace.
        InvalidBudgetError: if `monthly_budget_usd` is non-positive.
        AccountNameConflictError: if `name` is already taken.
    """
    _validate_name(name)
    _validate_budget(monthly_budget_usd)
    account = Account(name=name, monthly_budget_usd=monthly_budget_usd, is_operator=is_operator)
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AccountNameConflictError(f"account name {name!r} is already taken") from exc
    return account


async def rename_account(
    session: AsyncSession, account_id: int, new_name: str, *, commit: bool = True
) -> Account:
    """Rename an account and return it.

    Args:
        commit: When True (default), commits immediately and translates an
            `IntegrityError` (name collision) into `AccountNameConflictError`.
            When False, only validates/loads/mutates - no commit, and any
            `IntegrityError` is left untranslated for the caller to handle
            around its own commit (used by multi-field atomic updates such
            as the dashboard's PATCH /accounts/{id} route).

    Raises:
        AccountNotFoundError: if no account has that id.
        InvalidAccountNameError: if `new_name` is blank or all whitespace.
        AccountNameConflictError: if `new_name` is already taken (only when
            `commit` is True; the integrity check happens at commit time).
    """
    _validate_name(new_name)
    account = await _get_account_or_404(session, account_id)
    account.name = new_name
    if not commit:
        return account
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AccountNameConflictError(f"account name {new_name!r} is already taken") from exc
    return account


async def set_budget(
    session: AsyncSession, account_id: int, amount: float | None, *, commit: bool = True
) -> Account:
    """Set or clear an account's monthly spend cap and return it.

    Args:
        amount: Positive cap, or None to clear it (unlimited).
        commit: When True (default), commits immediately. When False, only
            validates/loads/mutates - no commit, leaving the caller to commit
            as part of a larger atomic update.

    Raises:
        AccountNotFoundError: if no account has that id.
        InvalidBudgetError: if `amount` is present but non-positive.
    """
    _validate_budget(amount)
    account = await _get_account_or_404(session, account_id)
    account.monthly_budget_usd = amount
    if commit:
        await session.commit()
    return account


async def set_operator(
    session: AsyncSession, account_id: int, value: bool, *, commit: bool = True
) -> Account:
    """Set an account's operator flag, guarding against removing the last operator.

    Args:
        value: The new operator flag.
        commit: When True (default), commits immediately. When False, only
            runs the guard/loads/mutates - no commit, leaving the caller to
            commit as part of a larger atomic update.

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
    if commit:
        await session.commit()
    return account


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


async def create_key(session: AsyncSession, account_id: int, name: str) -> tuple[ApiKey, str]:
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
        raise KeyNameConflictError(f"key name {name!r} is already used on this account") from exc
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


async def get_account_spend(session: AsyncSession, redis: Redis, account_id: int) -> float:
    """Return an account's current-period budget-relevant spend.

    Thin wrapper over `middleware.budget.get_period_spend` so callers never
    reach for `check_budget` (which would fire alerts on a dashboard read).
    """
    return await get_period_spend(session, redis, account_id=account_id)


async def list_accounts_with_stats(session: AsyncSession, redis: Redis) -> list[AccountStats]:
    """Return every account with key counts and month-to-date spend, by name.

    Key counts come from one grouped aggregate over api_keys; spend comes
    from `get_period_spend_batch` for all accounts at once (a single Redis
    MGET, falling back to one grouped DB aggregate for any misses) rather
    than a per-account round-trip.
    """
    accounts = (await session.execute(select(Account).order_by(Account.name))).scalars().all()
    count_rows = (
        await session.execute(
            select(
                ApiKey.account_id,
                func.count(ApiKey.id),
                func.coalesce(func.sum(func.cast(ApiKey.active, sa_Integer)), 0),
            ).group_by(ApiKey.account_id)
        )
    ).all()
    totals = {aid: int(total) for aid, total, _ in count_rows}
    actives = {aid: int(active) for aid, _, active in count_rows}

    spends = await get_period_spend_batch(
        session, redis, account_ids=[account.id for account in accounts]
    )

    stats: list[AccountStats] = []
    for account in accounts:
        stats.append(
            AccountStats(
                id=account.id,
                name=account.name,
                is_operator=account.is_operator,
                monthly_budget_usd=account.monthly_budget_usd,
                created_at=account.created_at,
                active_key_count=actives.get(account.id, 0),
                total_key_count=totals.get(account.id, 0),
                spend_mtd=spends.get(account.id, 0.0),
            )
        )
    return stats
