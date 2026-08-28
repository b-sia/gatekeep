import pytest

from gatekeep.accounts import auth_service
from gatekeep.storage.db import SessionLocal


@pytest.mark.asyncio
async def test_resend_unknown_email_returns_none():
    async with SessionLocal() as s:
        assert await auth_service.resend_verification_email(s, email="nobody@x.com") is None


@pytest.mark.asyncio
async def test_resend_issues_a_new_usable_token():
    async with SessionLocal() as s:
        account, _first_raw = await auth_service.signup(s, email="lost@x.com", password="pw123456")
        raw = await auth_service.resend_verification_email(s, email="lost@x.com")
        assert raw
        verified = await auth_service.verify_email(s, raw_token=raw)
        assert verified.id == account.id


@pytest.mark.asyncio
async def test_resend_for_already_verified_account_returns_none():
    async with SessionLocal() as s:
        _, raw = await auth_service.signup(s, email="done@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=raw)
        assert await auth_service.resend_verification_email(s, email="done@x.com") is None
