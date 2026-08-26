import pytest
from sqlalchemy import select

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_service import EmailConflictError, InvalidTokenError
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import AccountCredential


@pytest.mark.asyncio
async def test_signup_creates_pending_account_and_token():
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="A@B.com", password="pw123456")
        assert account.status == "pending"
        cred = (
            await s.execute(
                select(AccountCredential).where(AccountCredential.account_id == account.id)
            )
        ).scalar_one()
        assert cred.email == "a@b.com" and cred.email_verified is False
        assert raw  # verification token returned for the caller to email


@pytest.mark.asyncio
async def test_signup_duplicate_email_raises():
    async with SessionLocal() as s:
        await auth_service.signup(s, email="dup@x.com", password="pw123456")
        with pytest.raises(EmailConflictError):
            await auth_service.signup(s, email="dup@x.com", password="pw123456")


@pytest.mark.asyncio
async def test_verify_email_marks_verified_and_is_single_use():
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="v@x.com", password="pw123456")
        verified = await auth_service.verify_email(s, raw_token=raw)
        assert verified.id == account.id
        cred = (
            await s.execute(
                select(AccountCredential).where(AccountCredential.account_id == account.id)
            )
        ).scalar_one()
        assert cred.email_verified is True
        with pytest.raises(InvalidTokenError):
            await auth_service.verify_email(s, raw_token=raw)  # already used
