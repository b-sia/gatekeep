import pytest
from sqlalchemy import select

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import InvalidTokenError
from gatekeep.accounts.passwords import verify_password
from gatekeep.accounts.sessions import resolve_session_account
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import AccountCredential


async def _verified(s, email="r@x.com", pw="pw123456"):
    account, raw = await auth_service.signup(s, email=email, password=pw)
    await auth_service.verify_email(s, raw_token=raw)
    account.status = "approved"
    await s.commit()
    return account


@pytest.mark.asyncio
async def test_reset_request_unknown_email_returns_none():
    async with SessionLocal() as s:
        assert await auth_service.request_password_reset(s, email="nobody@x.com") is None


@pytest.mark.asyncio
async def test_reset_sets_password_and_revokes_sessions():
    async with SessionLocal() as s:
        acct = await _verified(s)
        _, sess = await auth_service.login(s, email="r@x.com", password="pw123456")
        token = await auth_service.request_password_reset(s, email="r@x.com")
        assert token
        await auth_service.reset_password(s, raw_token=token, new_password="newpw789")
        cred = (
            await s.execute(
                select(AccountCredential).where(AccountCredential.account_id == acct.id)
            )
        ).scalar_one()
        assert verify_password("newpw789", cred.password_hash)
        assert await resolve_session_account(s, sess) is None  # old sessions revoked


@pytest.mark.asyncio
async def test_reset_with_bad_token_raises():
    async with SessionLocal() as s:
        with pytest.raises(InvalidTokenError):
            await auth_service.reset_password(s, raw_token="bad", new_password="x")
