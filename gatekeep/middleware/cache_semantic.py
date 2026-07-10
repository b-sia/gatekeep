from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.api.openai_schemas import (
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gatekeep.api.translation import new_completion_id
from gatekeep.models import CachedResponse


def extract_embeddable_text(payload: dict[str, Any]) -> str:
    """Concatenate a payload's system text and user-message text for embedding.

    Assistant messages are excluded, matching the brief's "embed only
    user+system messages" rule.
    """
    parts: list[str] = []
    if "system" in payload:
        parts.append(payload["system"])
    for msg in payload["messages"]:
        if msg["role"] == "user":
            parts.append(msg["content"])
    return "\n\n".join(parts)


async def store_cached_response(
    session: AsyncSession,
    *,
    exact_hash: str,
    user_messages_text: str,
    embedding: list[float],
    response_text: str,
    model: str,
    cost_usd: float,
) -> CachedResponse:
    """Insert a new semantic-cache row and commit it."""
    row = CachedResponse(
        exact_hash=exact_hash,
        user_messages_text=user_messages_text,
        embedding=embedding,
        response_text=response_text,
        model=model,
        cost_usd=cost_usd,
    )
    session.add(row)
    await session.commit()
    return row


async def find_semantic_match(
    session: AsyncSession,
    embedding: list[float],
    *,
    threshold: float,
    max_age_seconds: int,
) -> CachedResponse | None:
    """Find the most similar non-expired cached response above `threshold`, or None.

    Similarity is cosine similarity (1 - cosine_distance). Rows older than
    `max_age_seconds` are excluded, matching the exact cache's TTL so both
    caches invalidate together.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    distance = CachedResponse.embedding.cosine_distance(embedding)
    stmt = (
        select(CachedResponse, distance.label("distance"))
        .where(CachedResponse.created_at >= cutoff)
        .order_by(distance)
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    cached, distance_value = row
    similarity = 1 - distance_value
    if similarity > threshold:
        return cached
    return None


def build_response_from_cache(cached: CachedResponse) -> ChatCompletionResponse:
    """Build a fresh ChatCompletionResponse from a semantic cache hit.

    Uses a new completion id/timestamp for this request, since the response
    was originally generated (and cached) for a different, merely similar,
    request. No token usage is stored on CachedResponse, so usage is
    reported as zero, reflecting that a semantic-cache hit consumes no
    provider tokens.
    """
    return ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=cached.model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content=cached.response_text),
                finish_reason="stop",
            )
        ],
        usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )
