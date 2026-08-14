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


def _redis_key(account_id: int, request_hash: str) -> str:
    """Build the namespaced, account-scoped Redis key for a request hash.

    Partitioning by account keeps one tenant's exact-cache hit
    from ever being served to another.
    """
    return f"{_KEY_PREFIX}{account_id}:{request_hash}"


def _member(account_id: int, request_hash: str) -> str:
    """Return the (account_id, request_hash) pair stored in a prompt's invalidation set."""
    return f"{account_id}:{request_hash}"


def _by_prompt_key(prompt_name: str) -> str:
    """Build the Redis key of the set indexing cache entries tagged with a prompt name."""
    return f"{_KEY_PREFIX}by-prompt:{prompt_name}"


async def get_cached_response(
    redis: Redis, account_id: int, request_hash: str
) -> ChatCompletionResponse | None:
    """Look up a cached response for `account_id` by request hash, or None on a miss."""
    raw = await redis.get(_redis_key(account_id, request_hash))
    if raw is None:
        return None
    return ChatCompletionResponse.model_validate_json(raw)


async def set_cached_response(
    redis: Redis,
    account_id: int,
    request_hash: str,
    response: ChatCompletionResponse,
    *,
    ttl_seconds: int,
    prompt_name: str | None = None,
) -> None:
    """Store a chat completion response in the account's exact-match cache with a TTL.

    If `prompt_name` is set, the (account_id, request_hash) pair is also added
    to a per-prompt Redis set (`cache:exact:by-prompt:{prompt_name}`) so a
    later prompt promotion can find and invalidate every exact-cache entry it
    produced, across all accounts.
    """
    await redis.set(
        _redis_key(account_id, request_hash), response.model_dump_json(), ex=ttl_seconds
    )
    if prompt_name is not None:
        await redis.sadd(_by_prompt_key(prompt_name), _member(account_id, request_hash))


async def clear_cached_response(redis: Redis, account_id: int, request_hash: str) -> None:
    """Manually invalidate one cached response for `account_id` by request hash."""
    await redis.delete(_redis_key(account_id, request_hash))


async def invalidate_prompt_cache(redis: Redis, prompt_name: str) -> None:
    """Delete every exact-cache entry tagged with `prompt_name`, across all accounts.

    Prompt promotion is a global operator action, so this spans
    tenants. The invalidation set stores `account_id:request_hash` members;
    each maps back to its account-scoped Redis key. No-op if no cache entries
    have ever been tagged with this prompt name.
    """
    index_key = _by_prompt_key(prompt_name)
    members = await redis.smembers(index_key)
    if members:
        await redis.delete(*(f"{_KEY_PREFIX}{m}" for m in members))
    await redis.delete(index_key)
