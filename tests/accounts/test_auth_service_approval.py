import pytest

from gatekeep.accounts import account_service, auth_service, sessions
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
        account, email, newly_approved = await auth_service.approve_account(
            s, account_id=a.id, monthly_budget_usd=25.0
        )
        assert account.status == "approved"
        assert account.monthly_budget_usd == 25.0
        assert email == "ap@x.com"
        assert newly_approved is True


@pytest.mark.asyncio
async def test_approve_is_idempotent_for_an_already_approved_account():
    async with SessionLocal() as s:
        a = await _pending(s, "dup@x.com")
        await auth_service.approve_account(s, account_id=a.id, monthly_budget_usd=25.0)

        # A duplicate approval (e.g. from a spam-clicked button) must not
        # re-trigger the "newly approved" signal the route uses to decide
        # whether to send another approval email, and must not clobber the
        # budget set by the first call.
        account, email, newly_approved = await auth_service.approve_account(
            s, account_id=a.id, monthly_budget_usd=999.0
        )
        assert account.monthly_budget_usd == 25.0
        assert email == "dup@x.com"
        assert newly_approved is False


@pytest.mark.asyncio
async def test_approve_raises_when_credential_row_not_yet_committed():
    # Reproduces the window between signup()'s two commits: the Account
    # exists but its AccountCredential hasn't landed yet. Approving in that
    # window must raise a handleable error, not crash with an uncaught
    # NoResultFound.
    async with SessionLocal() as s:
        account = await account_service.create_account(s, name="racer@x.com", status="pending")
        with pytest.raises(auth_service.CredentialsNotReadyError):
            await auth_service.approve_account(s, account_id=account.id, monthly_budget_usd=10.0)


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
