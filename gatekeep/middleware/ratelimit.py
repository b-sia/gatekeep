from __future__ import annotations

import math
import time

from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from gatekeep.config import Settings, get_settings
from gatekeep.middleware.auth import require_api_key
from gatekeep.models import ApiKey
from gatekeep.observability.metrics import rate_limit_rejections_total

# Atomically refills and consumes one token from a per-key bucket stored as a
# Redis hash ("tokens", "ts"). Doing the read-refill-consume sequence inside a
# single EVAL keeps it race-free across concurrent requests for the same key.
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local data = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then
    elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call("HMSET", key, "tokens", tostring(tokens), "ts", tostring(now))
redis.call("EXPIRE", key, 120)

return {allowed, tostring(tokens)}
"""

_redis: Redis | None = None


def get_redis(settings: Settings | None = None) -> Redis:
    """Return the process-wide async Redis client used for rate limiting.

    Lazily builds a client from `Settings.redis_url` on first use and reuses
    it afterwards. `settings` is only consulted on the first call.
    """
    global _redis
    if _redis is None:
        settings = settings or get_settings()
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


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
    allowed, tokens = await redis.eval(
        _TOKEN_BUCKET_SCRIPT,
        1,
        f"ratelimit:{account_id}",
        capacity,
        refill_rate,
        now if now is not None else time.time(),
    )
    return bool(int(allowed)), float(tokens)


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
