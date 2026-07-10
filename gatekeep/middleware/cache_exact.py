from __future__ import annotations

import hashlib
import json
from typing import Any

from redis.asyncio import Redis

from gatekeep.api.openai_schemas import ChatCompletionResponse

_KEY_PREFIX = "cache:exact:"


def hash_request(payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA256 hash of a completion payload's cacheable fields.

    Only `model`, `messages`, `max_tokens`, `system` (if set), and
    `stop_sequences` (if set) affect the hash. `max_tokens` is included
    because it bounds the response length; a truncated response cached
    under a low `max_tokens` must not be served to a request that allows
    a longer completion, and vice versa.
    """
    cacheable: dict[str, Any] = {
        "model": payload["model"],
        "messages": payload["messages"],
        "max_tokens": payload["max_tokens"],
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


def _by_prompt_key(prompt_name: str) -> str:
    """Build the Redis key of the set indexing cache hashes tagged with a prompt name."""
    return f"{_KEY_PREFIX}by-prompt:{prompt_name}"


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
    prompt_name: str | None = None,
) -> None:
    """Store a chat completion response in the exact-match cache with a TTL.

    If `prompt_name` is set, the request hash is also added to a per-prompt
    Redis set (`cache:exact:by-prompt:{prompt_name}`) so a later prompt
    promotion can find and invalidate every exact-cache entry it produced.
    """
    await redis.set(
        _redis_key(request_hash), response.model_dump_json(), ex=ttl_seconds
    )
    if prompt_name is not None:
        await redis.sadd(_by_prompt_key(prompt_name), request_hash)


async def clear_cached_response(redis: Redis, request_hash: str) -> None:
    """Manually invalidate one cached response by request hash."""
    await redis.delete(_redis_key(request_hash))


async def invalidate_prompt_cache(redis: Redis, prompt_name: str) -> None:
    """Delete every exact-cache entry tagged with `prompt_name`, plus its index set.

    No-op if no cache entries have ever been tagged with this prompt name.
    """
    index_key = _by_prompt_key(prompt_name)
    hashes = await redis.smembers(index_key)
    if hashes:
        await redis.delete(*(_redis_key(h) for h in hashes))
    await redis.delete(index_key)
