from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import RequestSample


async def record_request_sample(
    session: AsyncSession,
    *,
    key_id: int,
    prompt_name: str,
    model: str,
    input_messages: list[dict],
    output_text: str,
) -> RequestSample:
    """Persist one cache-miss request as a RequestSample and commit it.

    Only called for prompt-scoped, provider-served requests so the corpus
    stays a representative, append-only record of fresh traffic per prompt.
    """
    sample = RequestSample(
        key_id=key_id,
        prompt_name=prompt_name,
        model=model,
        input_messages=input_messages,
        output_text=output_text,
    )
    session.add(sample)
    await session.commit()
    return sample


async def recent_samples(
    prompt_name: str, session: AsyncSession, *, limit: int
) -> list[RequestSample]:
    """Return the most recent `limit` request samples for `prompt_name`, newest first."""
    result = await session.execute(
        select(RequestSample)
        .where(RequestSample.prompt_name == prompt_name)
        .order_by(RequestSample.created_at.desc(), RequestSample.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
