from __future__ import annotations

import time

from redis.asyncio import Redis

from gatekeep.config import Settings, get_settings

# Atomically refills and consumes one token from a bucket stored as a Redis
# hash ("tokens", "ts"). Doing the read-refill-consume sequence inside a
# single EVAL keeps it race-free across concurrent requests for the same key.
# Shared by both the per-account rate limiter (gatekeep.middleware.ratelimit)
# and the pre-auth per-IP rate limiter (gatekeep.middleware.auth), which
# otherwise couldn't share code without a circular import between the two
# (ratelimit imports require_api_key from auth).
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
    """Return the process-wide async Redis client shared by all token buckets.

    Lazily builds a client from `Settings.redis_url` on first use and reuses
    it afterwards. `settings` is only consulted on the first call.
    """
    global _redis
    if _redis is None:
        settings = settings or get_settings()
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def consume_token(
    redis: Redis,
    key: str,
    capacity: int,
    refill_rate: float,
    now: float | None = None,
) -> tuple[bool, float]:
    """Check and, if allowed, consume one token from the bucket stored at `key`.

    Runs the token-bucket refill/consume logic as a single Lua script so
    concurrent requests for the same `key` can't race. `now` defaults to the
    current time and is only overridable for tests.
    Returns (allowed, tokens_remaining_after_this_request).
    """
    allowed, tokens = await redis.eval(
        _TOKEN_BUCKET_SCRIPT,
        1,
        key,
        capacity,
        refill_rate,
        now if now is not None else time.time(),
    )
    return bool(int(allowed)), float(tokens)
