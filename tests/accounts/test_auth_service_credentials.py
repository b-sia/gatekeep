import pytest

from gatekeep.accounts import account_service, auth_service
from gatekeep.accounts.auth_service import CredentialsAlreadySetError, EmailConflictError
from gatekeep.storage.db import SessionLocal


@pytest.mark.asyncio
async def test_set_initial_credentials_creates_verified_credential_and_allows_login():
    async with SessionLocal() as s:
        account = await account_service.create_account(s, name="op-1", is_operator=True)
        await auth_service.set_initial_credentials(
            s, account_id=account.id, email="op@x.com", password="pw123456"
        )
        got, _raw = await auth_service.login(s, email="op@x.com", password="pw123456")
        assert got.id == account.id


@pytest.mark.asyncio
async def test_set_initial_credentials_twice_raises():
    async with SessionLocal() as s:
        account = await account_service.create_account(s, name="op-2")
        await auth_service.set_initial_credentials(
            s, account_id=account.id, email="op2@x.com", password="pw123456"
        )
        with pytest.raises(CredentialsAlreadySetError):
            await auth_service.set_initial_credentials(
                s, account_id=account.id, email="op2@x.com", password="pw123456"
            )


@pytest.mark.asyncio
async def test_set_initial_credentials_email_taken_by_another_account_raises():
    async with SessionLocal() as s:
        a1 = await account_service.create_account(s, name="op-3a")
        a2 = await account_service.create_account(s, name="op-3b")
        await auth_service.set_initial_credentials(
            s, account_id=a1.id, email="shared@x.com", password="pw123456"
        )
        with pytest.raises(EmailConflictError):
            await auth_service.set_initial_credentials(
                s, account_id=a2.id, email="shared@x.com", password="pw123456"
            )
