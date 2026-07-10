from __future__ import annotations

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from gatekeep.api.openai_schemas import ChatCompletionResponse

_KEY_PREFIX = "cache:exact:"


def hash_request(payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA256 hash of a completion payload's cacheable fields.

    Only `model`, `messages`, `system` (if set), and `stop_sequences` (if set)
    affect the hash, since those are the only fields that change the
    effective prompt; `max_tokens` and other bookkeeping fields are excluded
    so requests differing only in those still share a cache entry.
    """
    cacheable: dict[str, Any] = {
        "model": payload["model"],
        "messages": payload["messages"],
    }
    if "system" in payload:
        cacheable["system"] = payload["system"]
    if "stop_sequences" in payload:
        cacheable["stop_sequences"] = payload["stop_sequences"]
    encoded = json.dumps(cacheable, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redis_key(request_hash: str) -> str:
    """Build the namespaced Redis key for a request hash."""
    return f"{_KEY_PREFIX}{request_hash}"


async def get_cached_response(
    redis: Redis, request_hash: str
) -> ChatCompletionResponse | None:
    """Look up a cached chat completion response by request hash, or None on a miss."""
    raw = await redis.get(_redis_key(request_hash))
    if raw is None:
        return None
    return ChatCompletionResponse.model_validate_json(raw)


async def set_cached_response(
    redis: Redis,
    request_hash: str,
    response: ChatCompletionResponse,
    *,
    ttl_seconds: int,
) -> None:
    """Store a chat completion response in the exact-match cache with a TTL."""
    await redis.set(
        _redis_key(request_hash), response.model_dump_json(), ex=ttl_seconds
    )


async def clear_cached_response(redis: Redis, request_hash: str) -> None:
    """Manually invalidate one cached response by request hash."""
    await redis.delete(_redis_key(request_hash))
