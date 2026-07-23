from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.db import get_session
from gatekeep.middleware.auth import require_api_key
from gatekeep.models import (
    ApiKey,
    EvalRun,
    EvalSuite,
    Prompt,
    PromptVersion,
    RequestLog,
)
from gatekeep.prompts import PromptNotFoundError, _get_prompt_row

router = APIRouter(prefix="/dashboard/api", tags=["dashboard"])

_NO_PROMPT_LABEL = "(none)"


def _default_window() -> tuple[datetime, datetime]:
    """Return a (start, end) pair spanning the trailing 7 days up to now (UTC).

    Used as the default reporting window for endpoints where the caller
    doesn't supply explicit `start`/`end` query parameters.
    """
    end = datetime.now(timezone.utc)
    return end - timedelta(days=7), end


class UsageBreakdownRow(BaseModel):
    """One row of a cost/usage breakdown, grouped by a single dimension
    (model id, API key id, or prompt name)."""

    key: str
    request_count: int
    total_tokens: int
    cost_usd: float
    cache_hit_count: int


class UsageSummaryResponse(BaseModel):
    """Aggregate cost/usage totals over a time range, plus breakdowns by
    model, API key, and prompt name."""

    start: datetime
    end: datetime
    request_count: int
    total_tokens: int
    cost_usd: float
    cache_hit_count: int
    cache_hit_rate: float
    by_model: list[UsageBreakdownRow]
    by_key: list[UsageBreakdownRow]
    by_prompt: list[UsageBreakdownRow]


def _base_filters(
    start: datetime,
    end: datetime,
    *,
    model: str | None,
    key_id: int | None,
    prompt_name: str | None,
):
    """Build the shared list of SQLAlchemy WHERE clauses used by every usage
    query: the time range plus any of the optional model/key/prompt filters."""
    filters = [RequestLog.created_at >= start, RequestLog.created_at < end]
    if model is not None:
        filters.append(RequestLog.model == model)
    if key_id is not None:
        filters.append(RequestLog.key_id == key_id)
    if prompt_name is not None:
        filters.append(RequestLog.prompt_name == prompt_name)
    return filters


async def _breakdown(
    session: AsyncSession, group_col, filters: list
) -> list[UsageBreakdownRow]:
    """Run one GROUP BY aggregate over RequestLog for `group_col` and return
    the resulting rows as `UsageBreakdownRow`s, ordered by cost descending.

    `group_col` is a mapped column (e.g. `RequestLog.model`); its value is
    coerced to a string for the row's `key` field, with NULL rendered as
    `_NO_PROMPT_LABEL` (used for `by_prompt`, where a request may have no
    `prompt_name`).
    """
    rows = (
        await session.execute(
            select(
                group_col,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
            )
            .where(*filters)
            .group_by(group_col)
            .order_by(func.sum(RequestLog.cost_usd).desc())
        )
    ).all()
    return [
        UsageBreakdownRow(
            key=_NO_PROMPT_LABEL if value is None else str(value),
            request_count=count,
            total_tokens=int(total_tokens),
            cost_usd=float(cost_usd),
            cache_hit_count=int(cache_hits),
        )
        for value, count, total_tokens, cost_usd, cache_hits in rows
    ]


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> UsageSummaryResponse:
    """Return cost/usage totals over a time range, broken down by model, key,
    and prompt name.

    `start`/`end` default to the trailing 7 days when omitted. `model`,
    `key_id`, and `prompt_name` are optional equality filters applied on top
    of the time range. Requires a valid API key (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _base_filters(
        start, end, model=model, key_id=key_id, prompt_name=prompt_name
    )

    totals_row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
            ).where(*filters)
        )
    ).one()
    request_count, total_tokens, cost_usd, cache_hit_count = totals_row
    request_count = int(request_count)
    cache_hit_count = int(cache_hit_count)
    cache_hit_rate = (cache_hit_count / request_count) if request_count else 0.0

    by_model = await _breakdown(session, RequestLog.model, filters)
    by_key = await _breakdown(session, RequestLog.key_id, filters)
    by_prompt = await _breakdown(session, RequestLog.prompt_name, filters)

    return UsageSummaryResponse(
        start=start,
        end=end,
        request_count=request_count,
        total_tokens=int(total_tokens),
        cost_usd=float(cost_usd),
        cache_hit_count=cache_hit_count,
        cache_hit_rate=cache_hit_rate,
        by_model=by_model,
        by_key=by_key,
        by_prompt=by_prompt,
    )


class TimeseriesBucket(BaseModel):
    """One time bucket of request volume/cache-hit/cost data."""

    bucket_start: datetime
    request_count: int
    cache_hit_count: int
    cost_usd: float


class TimeseriesResponse(BaseModel):
    """Request volume/cache-hit-rate/cost, bucketed over a time range."""

    start: datetime
    end: datetime
    interval: str
    buckets: list[TimeseriesBucket]


@router.get("/usage/timeseries", response_model=TimeseriesResponse)
async def usage_timeseries(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    interval: Literal["hour", "day"] = Query(default="day"),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> TimeseriesResponse:
    """Return request volume, cache-hit count, and cost, bucketed by hour or day.

    `start`/`end` default to the trailing 7 days when omitted; `interval`
    selects the bucket width via Postgres `date_trunc`. Requires a valid API
    key (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _base_filters(
        start, end, model=model, key_id=key_id, prompt_name=prompt_name
    )

    bucket = func.date_trunc(interval, RequestLog.created_at)
    rows = (
        await session.execute(
            select(
                bucket,
                func.count(RequestLog.id),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(*filters)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    buckets = [
        TimeseriesBucket(
            bucket_start=bucket_start,
            request_count=int(count),
            cache_hit_count=int(cache_hits),
            cost_usd=float(cost_usd),
        )
        for bucket_start, count, cache_hits, cost_usd in rows
    ]
    return TimeseriesResponse(start=start, end=end, interval=interval, buckets=buckets)


class EvalRunOut(BaseModel):
    """One eval run, joined with its suite's prompt name and the prompt
    version number it evaluated."""

    id: int
    suite_id: int
    prompt_name: str
    prompt_version_id: int
    version_num: int
    model: str
    score: float
    passed: bool
    created_at: datetime


class EvalHistoryResponse(BaseModel):
    """A list of eval runs, newest first."""

    runs: list[EvalRunOut]


@router.get("/evals", response_model=EvalHistoryResponse)
async def eval_history(
    prompt_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> EvalHistoryResponse:
    """Return eval run history (score/pass-fail trend), newest first.

    Optionally filtered to a single prompt's suite via `prompt_name`.
    `limit` caps the number of runs returned (default 50, max 500). Requires
    a valid API key (`require_api_key`).
    """
    query = (
        select(EvalRun, EvalSuite.prompt_name, PromptVersion.version_num)
        .join(EvalSuite, EvalRun.suite_id == EvalSuite.id)
        .join(PromptVersion, EvalRun.prompt_version_id == PromptVersion.id)
        .order_by(EvalRun.created_at.desc())
        .limit(limit)
    )
    if prompt_name is not None:
        query = query.where(EvalSuite.prompt_name == prompt_name)

    rows = (await session.execute(query)).all()
    runs = [
        EvalRunOut(
            id=run.id,
            suite_id=run.suite_id,
            prompt_name=suite_prompt_name,
            prompt_version_id=run.prompt_version_id,
            version_num=version_num,
            model=run.model,
            score=run.score,
            passed=run.passed,
            created_at=run.created_at,
        )
        for run, suite_prompt_name, version_num in rows
    ]
    return EvalHistoryResponse(runs=runs)


class PromptOut(BaseModel):
    """One registered prompt, with its currently-active version number."""

    name: str
    active_version_num: int | None
    created_at: datetime
    updated_at: datetime


class PromptListResponse(BaseModel):
    """All registered prompts, ordered by name."""

    prompts: list[PromptOut]


@router.get("/prompts", response_model=PromptListResponse)
async def list_prompts_dashboard(
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> PromptListResponse:
    """List every registered prompt with its active version number, for the
    dashboard's prompt picker. Requires a valid API key (`require_api_key`)."""
    active_version = PromptVersion.__table__.alias("active_version")
    rows = (
        await session.execute(
            select(
                Prompt.name,
                active_version.c.version_num,
                Prompt.created_at,
                Prompt.updated_at,
            )
            .outerjoin(active_version, Prompt.active_version_id == active_version.c.id)
            .order_by(Prompt.name)
        )
    ).all()
    prompts = [
        PromptOut(
            name=name,
            active_version_num=version_num,
            created_at=created_at,
            updated_at=updated_at,
        )
        for name, version_num, created_at, updated_at in rows
    ]
    return PromptListResponse(prompts=prompts)


class PromptVersionOut(BaseModel):
    """One immutable version in a prompt's promotion timeline."""

    version_num: int
    active: bool
    created_at: datetime
    created_by: str | None
    notes: str | None


class PromptVersionTimelineResponse(BaseModel):
    """A prompt's full version timeline, ordered oldest-to-newest."""

    name: str
    versions: list[PromptVersionOut]


@router.get("/prompts/{name}/versions", response_model=PromptVersionTimelineResponse)
async def prompt_version_timeline(
    name: str,
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> PromptVersionTimelineResponse:
    """Return `name`'s full version timeline (creation time, author, notes,
    and which version is currently active), ordered oldest-to-newest.

    Raises a 404 if no prompt is registered under `name`. Requires a valid
    API key (`require_api_key`).
    """
    try:
        prompt = await _get_prompt_row(name, session)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rows = (
        (
            await session.execute(
                select(PromptVersion)
                .where(PromptVersion.prompt_id == prompt.id)
                .order_by(PromptVersion.version_num)
            )
        )
        .scalars()
        .all()
    )
    versions = [
        PromptVersionOut(
            version_num=v.version_num,
            active=v.active,
            created_at=v.created_at,
            created_by=v.created_by,
            notes=v.notes,
        )
        for v in rows
    ]
    return PromptVersionTimelineResponse(name=name, versions=versions)
