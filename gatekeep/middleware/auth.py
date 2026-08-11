from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.auth_keys import hash_key
from gatekeep.db import get_session
from gatekeep.models import ApiKey


def extract_bearer(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract the raw API key from an `Authorization` or `x-api-key` header.

    Prefers `Authorization`, stripping a leading `Bearer ` prefix if present.
    Falls back to `x-api-key`. Returns None if neither header is set.
    """
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            return authorization[len(prefix) :].strip()
        return authorization.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def _unauthorized(message: str) -> HTTPException:
    """Build a 401 HTTPException with an OpenAI-shaped error body."""
    return HTTPException(
        status_code=401,
        detail={"error": {"message": message, "type": "authentication_error", "code": None}},
    )


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    """FastAPI dependency that authenticates a request by API key.

    Looks up the hashed key in Postgres and raises a 401 HTTPException
    (OpenAI-shaped body) if the key is missing, unknown, or inactive.
    """
    raw = extract_bearer(authorization, x_api_key)
    if not raw:
        raise _unauthorized("Missing API key. Provide 'Authorization: Bearer <key>'.")
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    ).scalar_one_or_none()
    if row is None or not row.active:
        raise _unauthorized("Invalid or inactive API key.")
    return row
