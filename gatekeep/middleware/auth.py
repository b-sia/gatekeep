from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.auth_keys import hash_key
from gatekeep.db import get_session
from gatekeep.models import ApiKey


def extract_bearer(authorization: str | None, x_api_key: str | None) -> str | None:
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            return authorization[len(prefix):].strip()
        return authorization.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": {"message": message, "type": "authentication_error", "code": None}},
    )


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    raw = extract_bearer(authorization, x_api_key)
    if not raw:
        raise _unauthorized("Missing API key. Provide 'Authorization: Bearer <key>'.")
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    ).scalar_one_or_none()
    if row is None or not row.active:
        raise _unauthorized("Invalid or inactive API key.")
    return row
