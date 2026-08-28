import pytest

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import (
    AccountNotActiveError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
)
from gatekeep.accounts.sessions import resolve_session_account
from gatekeep.storage.db import SessionLocal


async def _signup_verified(s, email="u@x.com", pw="pw123456", status="approved"):
    account, raw = await auth_service.signup(s, email=email, password=pw)
    await auth_service.verify_email(s, raw_token=raw)
    account.status = status
    await s.commit()
    return account


@pytest.mark.asyncio
async def test_login_success_returns_session():
    async with SessionLocal() as s:
        acct = await _signup_verified(s)
        got, raw = await auth_service.login(s, email="u@x.com", password="pw123456")
        assert got.id == acct.id
        assert (await resolve_session_account(s, raw)).id == acct.id


@pytest.mark.asyncio
async def test_login_wrong_password_raises_invalid_credentials():
    async with SessionLocal() as s:
        await _signup_verified(s, email="w@x.com")
        with pytest.raises(InvalidCredentialsError):
            await auth_service.login(s, email="w@x.com", password="nope")


@pytest.mark.asyncio
async def test_login_unverified_and_disabled_are_refused():
    async with SessionLocal() as s:
        account, _ = await auth_service.signup(s, email="unv@x.com", password="pw123456")
        with pytest.raises(EmailNotVerifiedError):
            await auth_service.login(s, email="unv@x.com", password="pw123456")
        _ = await _signup_verified(s, email="dis@x.com", status="disabled")
        with pytest.raises(AccountNotActiveError):
            await auth_service.login(s, email="dis@x.com", password="pw123456")


@pytest.mark.asyncio
async def test_logout_revokes_session():
    async with SessionLocal() as s:
        await _signup_verified(s, email="lo@x.com")
        _, raw = await auth_service.login(s, email="lo@x.com", password="pw123456")
        await auth_service.logout(s, raw_session_token=raw)
        assert await resolve_session_account(s, raw) is None
