import pytest

from gatekeep.accounts import auth_service, sessions
from gatekeep.accounts.sessions import resolve_session_account
from gatekeep.storage.db import SessionLocal


async def _pending(s, email):
    account, _ = await auth_service.signup(s, email=email, password="pw123456")
    return account


@pytest.mark.asyncio
async def test_list_pending_only_returns_pending():
    async with SessionLocal() as s:
        await _pending(s, "p1@x.com")
        a2 = await _pending(s, "p2@x.com")
        await auth_service.approve_account(s, account_id=a2.id, monthly_budget_usd=10.0)
        pending = await auth_service.list_pending_accounts(s)
        emails = {a.name for a in pending}
        assert "p1@x.com" in emails and "p2@x.com" not in emails


@pytest.mark.asyncio
async def test_approve_sets_status_budget_and_returns_email():
    async with SessionLocal() as s:
        a = await _pending(s, "ap@x.com")
        account, email = await auth_service.approve_account(
            s, account_id=a.id, monthly_budget_usd=25.0
        )
        assert account.status == "approved"
        assert account.monthly_budget_usd == 25.0
        assert email == "ap@x.com"


@pytest.mark.asyncio
async def test_reject_sets_status_rejected():
    async with SessionLocal() as s:
        a = await _pending(s, "rj@x.com")
        account = await auth_service.reject_account(s, account_id=a.id)
        assert account.status == "rejected"


@pytest.mark.asyncio
async def test_reject_revokes_existing_sessions():
    async with SessionLocal() as s:
        a = await _pending(s, "rj2@x.com")
        # A pending account can't normally log in, but a session could
        # already exist (e.g. issued before an operator later rejects the
        # account); rejection must invalidate it immediately.
        raw = await sessions.create_session(s, a.id)
        assert (await resolve_session_account(s, raw)) is not None

        await auth_service.reject_account(s, account_id=a.id)

        assert await resolve_session_account(s, raw) is None
