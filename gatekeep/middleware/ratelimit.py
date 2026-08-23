from __future__ import annotations

import math

from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from gatekeep.config import get_settings
from gatekeep.middleware.auth import require_api_key
from gatekeep.models import ApiKey
from gatekeep.observability.metrics import rate_limit_rejections_total
from gatekeep.redis_token_bucket import consume_token, get_redis

__all__ = ["check_rate_limit", "get_redis", "require_rate_limit"]


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


def _rate_limiter_unavailable() -> HTTPException:
    """Build a 503 HTTPException with an OpenAI-shaped body for a rate-limiter outage.

    Rate limiting fails closed: if Redis is unreachable we can't verify a
    key's remaining budget, and letting requests through unchecked risks
    unbounded spend during the outage, so the request is rejected instead.
    """
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "message": "Rate limiter temporarily unavailable. Please retry shortly.",
                "type": "service_unavailable_error",
                "code": None,
            }
        },
    )


async def check_rate_limit(
    redis: Redis,
    account_id: int,
    capacity: int,
    refill_rate: float,
    now: float | None = None,
) -> tuple[bool, float]:
    """Check and, if allowed, consume one token from an account's Redis token bucket.

    Runs the token-bucket refill/consume logic as a single Lua script so
    concurrent requests for the same `account_id` can't race. Rate limiting
    is pooled at the account: every key on the account draws from
    one shared bucket. `now` defaults to the current time and is only
    overridable for tests.
    Returns (allowed, tokens_remaining_after_this_request).
    """
    return await consume_token(redis, f"ratelimit:{account_id}", capacity, refill_rate, now)


async def require_rate_limit(key: ApiKey = Depends(require_api_key)) -> ApiKey:
    """FastAPI dependency enforcing a per-account token-bucket rate limit.

    Chains after `require_api_key` so it has the resolved `ApiKey.account_id`
    to use as the Redis bucket key (rate limiting is pooled at the account).
    Raises `HTTPException(429)` with a Retry-After header when the
    account's bucket has no tokens left. Fails closed: if Redis itself is
    unreachable, raises `HTTPException(503)` rather than either silently
    letting the request through or crashing with an unhandled 500, since
    fail-open on spend controls during an outage is the bigger risk.
    """
    settings = get_settings()
    redis = get_redis(settings)
    capacity = settings.rate_limit_tokens_per_min
    refill_rate = settings.rate_limit_refill_rate

    try:
        allowed, tokens = await check_rate_limit(redis, key.account_id, capacity, refill_rate)
    except RedisError:
        raise _rate_limiter_unavailable() from None
    if not allowed:
        rate_limit_rejections_total.inc()
        deficit = 1 - tokens
        retry_after = max(1, math.ceil(deficit / refill_rate))
        raise _too_many_requests(retry_after)
    return key
