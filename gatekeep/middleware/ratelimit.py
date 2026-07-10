from __future__ import annotations

import math
import time

from fastapi import Depends, HTTPException
from redis.asyncio import Redis

from gatekeep.config import Settings, get_settings
from gatekeep.middleware.auth import require_api_key
from gatekeep.models import ApiKey

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


async def check_rate_limit(
    redis: Redis,
    key_id: int,
    capacity: int,
    refill_rate: float,
    now: float | None = None,
) -> tuple[bool, float]:
    """Check and, if allowed, consume one token from a key's Redis token bucket.

    Runs the token-bucket refill/consume logic as a single Lua script so
    concurrent requests for the same `key_id` can't race. `now` defaults to
    the current time and is only overridable for tests.
    Returns (allowed, tokens_remaining_after_this_request).
    """
    allowed, tokens = await redis.eval(
        _TOKEN_BUCKET_SCRIPT,
        1,
        f"ratelimit:{key_id}",
        capacity,
        refill_rate,
        now if now is not None else time.time(),
    )
    return bool(int(allowed)), float(tokens)


async def require_rate_limit(key: ApiKey = Depends(require_api_key)) -> ApiKey:
    """FastAPI dependency enforcing a per-key token-bucket rate limit.

    Chains after `require_api_key` so it has the resolved `ApiKey.id` to use
    as the Redis bucket key. Raises `HTTPException(429)` with a Retry-After
    header when the key's bucket has no tokens left.
    """
    settings = get_settings()
    redis = get_redis(settings)
    capacity = settings.rate_limit_tokens_per_min
    refill_rate = settings.rate_limit_refill_rate

    allowed, tokens = await check_rate_limit(redis, key.id, capacity, refill_rate)
    if not allowed:
        deficit = 1 - tokens
        retry_after = max(1, math.ceil(deficit / refill_rate))
        raise _too_many_requests(retry_after)
    return key
