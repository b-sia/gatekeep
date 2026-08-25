from __future__ import annotations

import math

from fastapi import Depends, Header, HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts.auth_keys import hash_key
from gatekeep.caching.redis_token_bucket import consume_token, get_redis
from gatekeep.config import get_settings
from gatekeep.observability.metrics import auth_failures_total, pre_auth_rate_limit_rejections_total
from gatekeep.storage.db import get_session
from gatekeep.storage.models import ApiKey


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


def _too_many_requests(retry_after: int) -> HTTPException:
    """Build a 429 HTTPException with an OpenAI-shaped body and Retry-After header."""
    return HTTPException(
        status_code=429,
        detail={
            "error": {
                "message": "Rate limit exceeded.",
                "type": "rate_limit_error",
                "code": None,
            }
        },
        headers={"Retry-After": str(retry_after)},
    )


async def _enforce_pre_auth_rate_limit(request: Request) -> None:
    """Enforce a coarse per-client-IP rate limit before any DB lookup runs.

    `require_api_key` below does a real hashed-key SELECT for every request,
    including ones with a missing or garbage token - and those never resolve
    to an account_id, so the per-account limiter in
    `gatekeep.middleware.ratelimit` (which only runs *after* auth succeeds)
    can't bound them. A flood of invalid tokens is otherwise a free, unmetered
    DB-load vector. This runs first and is keyed by `request.client.host`
    rather than any credential, so it catches that traffic before it reaches
    the database.

    Fails open on a Redis outage (unlike the post-auth limiter, which fails
    closed): this is an abuse backstop layered in front of real auth, not a
    spend control, so an outage here should degrade to "no extra protection"
    rather than take the whole gateway down.
    """
    client_host = request.client.host if request.client else "unknown"
    settings = get_settings()
    redis = get_redis(settings)
    capacity = settings.pre_auth_rate_limit_tokens_per_min
    refill_rate = settings.pre_auth_rate_limit_refill_rate
    try:
        allowed, tokens = await consume_token(
            redis, f"ratelimit:preauth:{client_host}", capacity, refill_rate
        )
    except RedisError:
        return
    if not allowed:
        pre_auth_rate_limit_rejections_total.inc()
        deficit = 1 - tokens
        retry_after = max(1, math.ceil(deficit / refill_rate))
        raise _too_many_requests(retry_after)


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
    _pre_auth: None = Depends(_enforce_pre_auth_rate_limit),
) -> ApiKey:
    """FastAPI dependency that authenticates a request by API key.

    Looks up the hashed key in Postgres and raises a 401 HTTPException
    (OpenAI-shaped body) if the key is missing, unknown, or inactive.
    Chains after `_enforce_pre_auth_rate_limit` (a per-IP token bucket) so a
    flood of missing/invalid tokens is throttled before it reaches the DB
    lookup below - see that function's docstring.
    """
    raw = extract_bearer(authorization, x_api_key)
    if not raw:
        auth_failures_total.inc()
        raise _unauthorized("Missing API key. Provide 'Authorization: Bearer <key>'.")
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    ).scalar_one_or_none()
    if row is None or not row.active:
        auth_failures_total.inc()
        raise _unauthorized("Invalid or inactive API key.")
    return row
