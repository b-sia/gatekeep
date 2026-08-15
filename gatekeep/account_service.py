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
    account = Account(name=name, monthly_budget_usd=monthly_budget_usd, is_operator=is_operator)
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
        raise AccountNameConflictError(f"account name {new_name!r} is already taken") from exc
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
