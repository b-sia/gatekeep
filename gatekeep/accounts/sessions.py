from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts.tokens import hash_token, new_token
from gatekeep.config import get_settings
from gatekeep.storage.models import Account, Session


async def create_session(session: AsyncSession, account_id: int) -> str:
    """Create a session row for an account and return its raw (unhashed) token."""
    raw = new_token()
    ttl = get_settings().session_ttl_seconds
    session.add(
        Session(
            token_hash=hash_token(raw),
            account_id=account_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )
    )
    await session.commit()
    return raw


async def resolve_session_account(session: AsyncSession, raw_token: str) -> Account | None:
    """Resolve a raw session token to its Account, or None if missing/expired.

    Refreshes `last_seen_at` on a valid hit.
    """
    row = (
        await session.execute(select(Session).where(Session.token_hash == hash_token(raw_token)))
    ).scalar_one_or_none()
    if row is None or row.expires_at <= datetime.now(UTC):
        return None
    row.last_seen_at = datetime.now(UTC)
    await session.commit()
    return await session.get(Account, row.account_id)


async def revoke_session(session: AsyncSession, raw_token: str) -> None:
    """Delete the session row for a raw token, if present (logout)."""
    await session.execute(delete(Session).where(Session.token_hash == hash_token(raw_token)))
    await session.commit()


async def revoke_account_sessions(session: AsyncSession, account_id: int) -> None:
    """Delete all sessions for an account (used on password reset)."""
    await session.execute(delete(Session).where(Session.account_id == account_id))
    await session.commit()
