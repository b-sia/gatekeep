import datetime as dt

import pytest
from sqlalchemy import select

from gatekeep.accounts.sessions import (
    create_session,
    resolve_session_account,
    revoke_account_sessions,
    revoke_session,
)
from gatekeep.accounts.tokens import hash_token
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import Session as SessionRow
from tests.helpers import create_account


@pytest.mark.asyncio
async def test_create_and_resolve_roundtrip():
    async with SessionLocal() as s:
        acct = await create_account(s)
        await s.commit()
        raw = await create_session(s, acct.id)
        resolved = await resolve_session_account(s, raw)
        assert resolved is not None and resolved.id == acct.id


@pytest.mark.asyncio
async def test_expired_session_resolves_to_none():
    async with SessionLocal() as s:
        acct = await create_account(s)
        await s.commit()
        raw = await create_session(s, acct.id)
        row = (
            await s.execute(select(SessionRow).where(SessionRow.token_hash == hash_token(raw)))
        ).scalar_one()
        row.expires_at = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
        await s.commit()
        assert await resolve_session_account(s, raw) is None


@pytest.mark.asyncio
async def test_revoke_session_and_revoke_all():
    async with SessionLocal() as s:
        acct = await create_account(s)
        await s.commit()
        raw = await create_session(s, acct.id)
        await revoke_session(s, raw)
        assert await resolve_session_account(s, raw) is None
        r2 = await create_session(s, acct.id)
        await revoke_account_sessions(s, acct.id)
        assert await resolve_session_account(s, r2) is None
