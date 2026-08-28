import pytest

from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import AccountCredential, EmailToken, Session
from tests.helpers import create_account


@pytest.mark.asyncio
async def test_account_status_defaults_to_approved():
    async with SessionLocal() as s:
        account = await create_account(s)
        await s.commit()
        await s.refresh(account)
        assert account.status == "approved"


@pytest.mark.asyncio
async def test_credential_session_and_token_persist():
    async with SessionLocal() as s:
        account = await create_account(s, status="pending")
        s.add(
            AccountCredential(
                account_id=account.id,
                email="a@b.com",
                password_hash="x",
                email_verified=False,
            )
        )
        s.add(
            Session(
                token_hash="th",
                account_id=account.id,
                expires_at=__import__("datetime").datetime(2999, 1, 1),
            )
        )
        s.add(
            EmailToken(
                purpose="verify_email",
                token_hash="et",
                account_id=account.id,
                expires_at=__import__("datetime").datetime(2999, 1, 1),
            )
        )
        await s.commit()
        cred = await s.get(AccountCredential, 1)
        assert cred.email == "a@b.com" and cred.email_verified is False
