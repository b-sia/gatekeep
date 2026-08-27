from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts import account_service, sessions
from gatekeep.accounts.account_service import AccountServiceError
from gatekeep.accounts.passwords import hash_password, verify_password
from gatekeep.accounts.tokens import hash_token, new_token
from gatekeep.config import get_settings
from gatekeep.storage.models import Account, AccountCredential, EmailToken


class EmailConflictError(AccountServiceError):
    """Raised when an email is already registered."""


class InvalidTokenError(AccountServiceError):
    """Raised when an email token is unknown, expired, or already used."""


class InvalidCredentialsError(AccountServiceError):
    """Raised when email/password authentication fails."""


class EmailNotVerifiedError(AccountServiceError):
    """Raised when a user logs in before verifying their email."""


class AccountNotActiveError(AccountServiceError):
    """Raised when a rejected or disabled account attempts to log in."""


class CredentialsAlreadySetError(AccountServiceError):
    """Raised when an account already has login credentials."""


# Precomputed once at import time so the "no such credential" branch of
# `login` can run a real bcrypt comparison against a constant hash. Without
# this, an unknown email would skip `verify_password` entirely while a known
# email with a wrong password would run it, and the resulting timing gap lets
# an attacker enumerate registered emails by measuring response latency.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing")


def _normalize_email(email: str) -> str:
    """Lowercase and strip an email for consistent storage and lookup."""
    return email.strip().lower()


async def _issue_email_token(session: AsyncSession, account_id: int, purpose: str) -> str:
    """Create a single-use email token row and return its raw value."""
    raw = new_token()
    ttl = get_settings().email_token_ttl_seconds
    session.add(
        EmailToken(
            purpose=purpose,
            token_hash=hash_token(raw),
            account_id=account_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
    )
    await session.commit()
    return raw


async def _consume_token(session: AsyncSession, raw_token: str, purpose: str) -> EmailToken:
    """Load a valid, unused, unexpired token of `purpose`, mark it used, and return it.

    Raises:
        InvalidTokenError: if the token is unknown, wrong purpose, expired, or used.
    """
    row = (
        await session.execute(
            select(EmailToken).where(EmailToken.token_hash == hash_token(raw_token))
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.purpose != purpose or row.used_at is not None or row.expires_at <= now:
        raise InvalidTokenError("invalid or expired token")
    row.used_at = now
    return row


async def signup(session: AsyncSession, *, email: str, password: str) -> tuple[Account, str]:
    """Register a pending account with credentials and return (account, raw_verify_token).

    Raises:
        EmailConflictError: if the email is already registered.
    """
    normalized = _normalize_email(email)
    existing = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.email == normalized)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise EmailConflictError("email already registered")
    try:
        account = await account_service.create_account(session, name=normalized, status="pending")
    except account_service.AccountNameConflictError as exc:
        # `Account.name` is the normalized email and is UNIQUE, so a
        # concurrent duplicate signup that raced past the `existing` check
        # above can still collide here. Surface it as the same
        # `EmailConflictError` the route already handles (identical 202),
        # rather than a raw `AccountNameConflictError` that would 500.
        raise EmailConflictError("email already registered") from exc
    session.add(
        AccountCredential(
            account_id=account.id,
            email=normalized,
            password_hash=hash_password(password),
            email_verified=False,
        )
    )
    await session.commit()
    raw = await _issue_email_token(session, account.id, "verify_email")
    return account, raw


async def verify_email(session: AsyncSession, *, raw_token: str) -> Account:
    """Consume a verify_email token and mark the credential verified.

    Raises:
        InvalidTokenError: if the token is invalid/expired/used.
    """
    token = await _consume_token(session, raw_token, "verify_email")
    cred = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.account_id == token.account_id)
        )
    ).scalar_one()
    cred.email_verified = True
    await session.commit()
    return await session.get(Account, token.account_id)


async def set_initial_credentials(
    session: AsyncSession, *, account_id: int, email: str, password: str
) -> AccountCredential:
    """Create login credentials for an account that has none yet.

    Bootstrap path for accounts created outside self-serve signup (CLI/script
    operators): the credential is marked verified immediately, since there is
    no signup flow here for a verification link to belong to.

    Raises:
        CredentialsAlreadySetError: if the account already has credentials.
        EmailConflictError: if the email is already registered to another
            account.
    """
    existing_for_account = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.account_id == account_id)
        )
    ).scalar_one_or_none()
    if existing_for_account is not None:
        raise CredentialsAlreadySetError(f"account {account_id} already has login credentials")
    normalized = _normalize_email(email)
    existing_for_email = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.email == normalized)
        )
    ).scalar_one_or_none()
    if existing_for_email is not None:
        raise EmailConflictError("email already registered")
    cred = AccountCredential(
        account_id=account_id,
        email=normalized,
        password_hash=hash_password(password),
        email_verified=True,
    )
    session.add(cred)
    await session.commit()
    return cred


async def login(session: AsyncSession, *, email: str, password: str) -> tuple[Account, str]:
    """Authenticate email/password and return (account, raw_session_token).

    Raises:
        InvalidCredentialsError: unknown email or wrong password.
        EmailNotVerifiedError: credential exists but email is unverified.
        AccountNotActiveError: account status is rejected or disabled.
    """
    normalized = _normalize_email(email)
    cred = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.email == normalized)
        )
    ).scalar_one_or_none()
    if cred is None:
        # Do the same bcrypt work as the "wrong password" path below so a
        # timing measurement can't distinguish "no such account" from "wrong
        # password" - see the `_DUMMY_PASSWORD_HASH` comment above.
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise InvalidCredentialsError("invalid email or password")
    if not verify_password(password, cred.password_hash):
        raise InvalidCredentialsError("invalid email or password")
    if not cred.email_verified:
        raise EmailNotVerifiedError("email not verified")
    account = await session.get(Account, cred.account_id)
    if account.status in ("rejected", "disabled"):
        raise AccountNotActiveError("account is not active")
    raw = await sessions.create_session(session, account.id)
    return account, raw


async def logout(session: AsyncSession, *, raw_session_token: str) -> None:
    """Revoke the given session token (idempotent)."""
    await sessions.revoke_session(session, raw_session_token)


async def resend_verification_email(session: AsyncSession, *, email: str) -> str | None:
    """Issue a fresh verify_email token, or None if there is nothing to send.

    Returns None (rather than raising) for an unknown email, and also for an
    account that is already verified, so the route can respond identically
    in all three cases (unknown, already verified, resent) - no enumeration.
    """
    normalized = _normalize_email(email)
    cred = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.email == normalized)
        )
    ).scalar_one_or_none()
    if cred is None or cred.email_verified:
        return None
    return await _issue_email_token(session, cred.account_id, "verify_email")


async def request_password_reset(session: AsyncSession, *, email: str) -> str | None:
    """Issue a reset token for an email, or None if no credential exists.

    Returns None (rather than raising) so the route can respond identically
    whether or not the email is registered (no enumeration).
    """
    normalized = _normalize_email(email)
    cred = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.email == normalized)
        )
    ).scalar_one_or_none()
    if cred is None:
        return None
    return await _issue_email_token(session, cred.account_id, "reset_password")


async def reset_password(session: AsyncSession, *, raw_token: str, new_password: str) -> Account:
    """Consume a reset token, set the new password, and revoke all sessions.

    Raises:
        InvalidTokenError: if the token is invalid/expired/used.
    """
    token = await _consume_token(session, raw_token, "reset_password")
    cred = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.account_id == token.account_id)
        )
    ).scalar_one()
    cred.password_hash = hash_password(new_password)
    await session.commit()
    await sessions.revoke_account_sessions(session, token.account_id)
    return await session.get(Account, token.account_id)


async def list_pending_accounts(session: AsyncSession) -> list[Account]:
    """Return all accounts awaiting operator approval, oldest first."""
    return list(
        (
            await session.execute(
                select(Account).where(Account.status == "pending").order_by(Account.created_at)
            )
        ).scalars()
    )


async def approve_account(
    session: AsyncSession, *, account_id: int, monthly_budget_usd: float | None
) -> tuple[Account, str, bool]:
    """Approve a pending account and set its budget.

    Idempotent: an account that is already approved is left untouched (its
    budget is not overwritten by a second call) so that a duplicate request
    - e.g. a caller double-submitting the approval - does not trigger a
    second approval email.

    Returns:
        A tuple of (account, credential email, whether this call newly
        approved the account). The caller should only send the approval
        email when the third element is `True`.

    Raises:
        AccountNotFoundError: if no account has that id.
    """
    account = await account_service.get_account(session, account_id)
    cred = (
        await session.execute(
            select(AccountCredential).where(AccountCredential.account_id == account_id)
        )
    ).scalar_one()
    if account.status == "approved":
        return account, cred.email, False
    account.status = "approved"
    account.monthly_budget_usd = monthly_budget_usd
    await session.commit()
    return account, cred.email, True


async def reject_account(session: AsyncSession, *, account_id: int) -> Account:
    """Mark an account rejected and revoke any sessions it already holds.

    Raises:
        AccountNotFoundError: if no account has that id.
    """
    account = await account_service.get_account(session, account_id)
    account.status = "rejected"
    await session.commit()
    await sessions.revoke_account_sessions(session, account_id)
    return account
