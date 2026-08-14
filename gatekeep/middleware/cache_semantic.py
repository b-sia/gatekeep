from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
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
    account_id: int,
    exact_hash: str,
    user_messages_text: str,
    embedding: list[float],
    response_text: str,
    model: str,
    cost_usd: float,
    prompt_name: str | None = None,
    prompt_version_num: int | None = None,
) -> CachedResponse | None:
    """Insert a new semantic-cache row and commit it.

    `account_id` scopes the row to its tenant (decision 1); the cache is
    partitioned per account, so exact_hash is unique per (account_id,
    exact_hash) rather than globally.

    If a row with the same `(account_id, exact_hash)` was inserted concurrently
    by another request (unique-constraint violation), rolls back and returns
    None instead of raising, since the cache write is best-effort and the
    original request's response has already been served. `prompt_name`, if
    set, tags the row so a later prompt promotion can find and delete it.
    `prompt_version_num`, if set, additionally tags the row with which
    PromptVersion actually generated it (active or A/B candidate), so
    `find_semantic_match` can scope matches to the version resolved for the
    current request - see that function's docstring for why this matters.
    """
    row = CachedResponse(
        account_id=account_id,
        exact_hash=exact_hash,
        user_messages_text=user_messages_text,
        embedding=embedding,
        response_text=response_text,
        model=model,
        cost_usd=cost_usd,
        prompt_name=prompt_name,
        prompt_version_num=prompt_version_num,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    return row


async def delete_cached_responses_by_prompt(session: AsyncSession, prompt_name: str) -> None:
    """Delete every semantic-cache row tagged with `prompt_name`.

    Does not commit; the caller (promote_prompt) commits this alongside its
    own version-pointer change so both updates land in one transaction.
    No-op if no rows are tagged with this prompt name.
    """
    await session.execute(delete(CachedResponse).where(CachedResponse.prompt_name == prompt_name))


@dataclass
class SemanticMatch:
    """A semantic-cache hit: the matched row plus its cosine similarity score."""

    cached: CachedResponse
    similarity: float


async def find_semantic_match(
    session: AsyncSession,
    embedding: list[float],
    *,
    account_id: int,
    model: str,
    threshold: float,
    max_age_seconds: int,
    prompt_version_num: int | None = None,
) -> SemanticMatch | None:
    """Find the most similar non-expired cached response above `threshold`, or None.

    Similarity is cosine similarity (1 - cosine_distance). Rows older than
    `max_age_seconds` are excluded, matching the exact cache's TTL so both
    caches invalidate together. Only rows cached for the same `model` are
    considered, so a semantically-similar prompt never returns a different
    model's cached answer. Only rows belonging to `account_id` are considered
    (decision 1), so a match never crosses tenants.

    If `prompt_version_num` is given (the caller resolved a `prompt_name` to
    a specific PromptVersion for this request), only rows tagged with that
    exact `prompt_version_num` are considered. This closes a version-mixing
    gap that A/B testing candidates introduce: without it, two rows tagged
    with the same `prompt_name` but generated from two different templates
    (the active version and an in-flight candidate) would be indistinguishable
    to a plain embedding-similarity match whenever the two templates are only
    a small wording tweak apart - exactly the case a real A/B test is likely
    to produce. Rows written before this column existed (`prompt_version_num`
    is NULL) never match here, by ordinary SQL NULL-comparison semantics -
    the conservative default of a few extra cache misses right after
    upgrading, rather than ever risking a version-mismatched hit. When
    `prompt_version_num` is None (no `prompt_name` on this request), behavior
    is unchanged from before this parameter existed.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
    distance = CachedResponse.embedding.cosine_distance(embedding)
    stmt = (
        select(CachedResponse, distance.label("distance"))
        .where(CachedResponse.account_id == account_id)
        .where(CachedResponse.created_at >= cutoff)
        .where(CachedResponse.model == model)
    )
    if prompt_version_num is not None:
        stmt = stmt.where(CachedResponse.prompt_version_num == prompt_version_num)
    stmt = stmt.order_by(distance).limit(1)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    cached, distance_value = row
    similarity = 1 - distance_value
    if similarity > threshold:
        return SemanticMatch(cached=cached, similarity=similarity)
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
