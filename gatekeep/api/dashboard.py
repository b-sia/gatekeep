from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Integer, case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.db import get_session
from gatekeep.middleware.auth import require_api_key
from gatekeep.models import (
    Account,
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
_FAILED_OUTCOMES = ("provider_error", "client_disconnect")


async def _require_caller_account(
    caller: ApiKey = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> Account:
    """Resolve the authenticated key's Account, for account-scoped dashboards.

    `account_id` is always derived server-side from the authenticated key,
    never accepted as a client-supplied parameter (decision 6, problem 1).
    """
    return await session.get(Account, caller.account_id)


def _account_scope(caller_account: Account) -> list:
    """Return the WHERE clauses restricting a usage query to the caller's account.

    A non-operator account sees only its own rows (decision 6); an operator
    account sees the whole fleet, so this returns no clause. The scope is
    ANDed onto every query, so a non-operator passing another account's
    `key_id` filter gets an empty result rather than a cross-tenant read.
    """
    if caller_account.is_operator:
        return []
    return [RequestLog.account_id == caller_account.id]


def _default_window() -> tuple[datetime, datetime]:
    """Return a (start, end) pair spanning the trailing 7 days up to now (UTC).

    Used as the default reporting window for endpoints where the caller
    doesn't supply explicit `start`/`end` query parameters.
    """
    end = datetime.now(UTC)
    return end - timedelta(days=7), end


class UsageBreakdownRow(BaseModel):
    """One row of a cost/usage breakdown, grouped by a single dimension
    (model id, API key id, or prompt name)."""

    key: str
    label: str | None = None
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
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    spend_usd: float
    savings_usd: float
    cache_hit_count: int
    cache_hit_rate: float
    failed_count: int
    success_rate: float
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


async def _breakdown(session: AsyncSession, group_col, filters: list) -> list[UsageBreakdownRow]:
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


async def _key_breakdown(session: AsyncSession, filters: list) -> list[UsageBreakdownRow]:
    """Run the same aggregate as `_breakdown` grouped by `RequestLog.key_id`,
    but also join `ApiKey` to attach each key's display name as `label`.

    Uses an outer join so requests from a since-deleted API key still show
    up, with `label` falling back to `#<id>`.
    """
    rows = (
        await session.execute(
            select(
                RequestLog.key_id,
                ApiKey.name,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
            )
            .outerjoin(ApiKey, RequestLog.key_id == ApiKey.id)
            .where(*filters)
            .group_by(RequestLog.key_id, ApiKey.name)
            .order_by(func.sum(RequestLog.cost_usd).desc())
        )
    ).all()
    return [
        UsageBreakdownRow(
            key=str(key_id),
            label=name if name is not None else f"#{key_id}",
            request_count=count,
            total_tokens=int(total_tokens),
            cost_usd=float(cost_usd),
            cache_hit_count=int(cache_hits),
        )
        for key_id, name, count, total_tokens, cost_usd, cache_hits in rows
    ]


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
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
    filters = _base_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name)
    filters += _account_scope(caller_account)

    totals_row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(case((RequestLog.cached, 0.0), else_=RequestLog.cost_usd)),
                    0.0,
                ),
                func.coalesce(
                    func.sum(case((RequestLog.cached, RequestLog.cost_usd), else_=0.0)),
                    0.0,
                ),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((RequestLog.outcome.in_(_FAILED_OUTCOMES), 1), else_=0)),
                    0,
                ),
            ).where(*filters)
        )
    ).one()
    (
        request_count,
        total_tokens,
        prompt_tokens,
        completion_tokens,
        cost_usd,
        spend_usd,
        savings_usd,
        cache_hit_count,
        failed_count,
    ) = totals_row
    request_count = int(request_count)
    cache_hit_count = int(cache_hit_count)
    failed_count = int(failed_count)
    # A cache hit is only ever served on a successful request, so the hit rate
    # is taken over successful requests, not the full count. Since #17 began
    # logging failed rows, dividing by request_count would silently deflate the
    # rate whenever upstream failures rise, with no change in caching behavior.
    successful_count = request_count - failed_count
    cache_hit_rate = (cache_hit_count / successful_count) if successful_count else 0.0
    success_rate = successful_count / request_count if request_count else 0.0

    by_model = await _breakdown(session, RequestLog.model, filters)
    by_key = await _key_breakdown(session, filters)
    by_prompt = await _breakdown(session, RequestLog.prompt_name, filters)

    return UsageSummaryResponse(
        start=start,
        end=end,
        request_count=request_count,
        total_tokens=int(total_tokens),
        prompt_tokens=int(prompt_tokens),
        completion_tokens=int(completion_tokens),
        cost_usd=float(cost_usd),
        spend_usd=float(spend_usd),
        savings_usd=float(savings_usd),
        cache_hit_count=cache_hit_count,
        cache_hit_rate=cache_hit_rate,
        failed_count=failed_count,
        success_rate=success_rate,
        by_model=by_model,
        by_key=by_key,
        by_prompt=by_prompt,
    )


class TimeseriesBucket(BaseModel):
    """One time bucket of request volume/cache-hit/cost/token data."""

    bucket_start: datetime
    request_count: int
    cache_hit_count: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    spend_usd: float
    savings_usd: float


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
    interval: Literal["minute", "hour", "day"] = Query(default="day"),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> TimeseriesResponse:
    """Return request volume, cache-hit count, cost, and token/spend/savings
    totals, bucketed by minute, hour, or day.

    `start`/`end` default to the trailing 7 days when omitted; `interval`
    selects the bucket width via Postgres `date_trunc`. Requires a valid API
    key (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _base_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name)
    filters += _account_scope(caller_account)

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
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(
                    func.sum(case((RequestLog.cached, RequestLog.total_tokens), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((RequestLog.cached, 0.0), else_=RequestLog.cost_usd)),
                    0.0,
                ),
                func.coalesce(
                    func.sum(case((RequestLog.cached, RequestLog.cost_usd), else_=0.0)),
                    0.0,
                ),
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
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            cached_tokens=int(cached_tokens),
            spend_usd=float(spend_usd),
            savings_usd=float(savings_usd),
        )
        for (
            bucket_start,
            count,
            cache_hits,
            cost_usd,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            spend_usd,
            savings_usd,
        ) in rows
    ]
    return TimeseriesResponse(start=start, end=end, interval=interval, buckets=buckets)


class UsageByModelBucket(BaseModel):
    """One (time bucket, model) row of request/token/cost totals."""

    bucket_start: datetime
    model: str
    request_count: int
    total_tokens: int
    cost_usd: float


class UsageByModelTimeseriesResponse(BaseModel):
    """Usage bucketed by both time and model, as a flat list of rows.

    Kept flat (rather than nested by model) so the response shape stays
    simple regardless of how many distinct models appear in the window;
    callers group by `model` themselves.
    """

    start: datetime
    end: datetime
    interval: str
    rows: list[UsageByModelBucket]


@router.get("/usage/timeseries/by-model", response_model=UsageByModelTimeseriesResponse)
async def usage_timeseries_by_model(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    interval: Literal["minute", "hour", "day"] = Query(default="day"),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> UsageByModelTimeseriesResponse:
    """Return request volume, tokens, and cost, bucketed by both time and
    model, for the per-model usage-over-time panel.

    Same filters and defaults as `usage_timeseries`. Requires a valid API
    key (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _base_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name)
    filters += _account_scope(caller_account)

    bucket = func.date_trunc(interval, RequestLog.created_at)
    rows = (
        await session.execute(
            select(
                bucket,
                RequestLog.model,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(*filters)
            .group_by(bucket, RequestLog.model)
            .order_by(bucket, RequestLog.model)
        )
    ).all()

    by_model_rows = [
        UsageByModelBucket(
            bucket_start=bucket_start,
            model=model_name,
            request_count=int(count),
            total_tokens=int(total_tokens),
            cost_usd=float(cost_usd),
        )
        for bucket_start, model_name, count, total_tokens, cost_usd in rows
    ]
    return UsageByModelTimeseriesResponse(
        start=start, end=end, interval=interval, rows=by_model_rows
    )


# Latency query building blocks -------------------------------------------
#
# `duration_ms` holds two different quantities depending on path: end-to-end
# on cache_exact/cache_semantic/provider, and time-to-last-token on stream
# (models.py). `provider_ms` splits the same way (one call vs. a whole
# stream), and overhead inherits the split from both. A percentile blended
# across the two would be meaningless, so every top-level figure is computed
# over one side or the other, never both.

_NON_STREAMING = RequestLog.path != "stream"
_STREAMING = RequestLog.path == "stream"

# On a cache hit no provider call was made, so the entire duration is
# gatekeep's own time. This matches the middleware's treatment of the same
# case (gatekeep/observability/latency.py).
#
# Gated on `cached` rather than `COALESCE(provider_ms, 0)`: a NULL
# `provider_ms` on a *non*-cached row means the upstream call never
# completed (see `test_provider_error_does_not_count_whole_span_as_overhead`
# on the Prometheus side), and today such rows are never logged at all - but
# that is a fact about the caller, not something this expression should rely
# on. If a failed request is ever logged (#17), `CASE` still returns NULL for
# it here, which drops it from the percentile set instead of silently
# attributing the whole span to gateway overhead.
_OVERHEAD_MS = case(
    (RequestLog.cached.is_(True), RequestLog.duration_ms),
    else_=RequestLog.duration_ms - RequestLog.provider_ms,
)

_QUANTILES = (0.5, 0.95, 0.99)


class Percentiles(BaseModel):
    """p50/p95/p99 of one latency quantity, in milliseconds."""

    p50_ms: float
    p95_ms: float
    p99_ms: float


class LatencyBreakdownRow(BaseModel):
    """One row of a latency breakdown, grouped by a single dimension
    (path, model id, API key id, or prompt name).

    `p50_ms`/`p95_ms` are None when the group has no qualifying rows, so a
    caller renders "-" rather than a misleading 0.
    """

    key: str
    label: str | None = None
    sample_count: int
    p50_ms: float | None
    p95_ms: float | None


def _latency_filters(
    start: datetime,
    end: datetime,
    *,
    model: str | None,
    key_id: int | None,
    prompt_name: str | None,
) -> list:
    """Build the WHERE clauses for a latency query: the usual usage filters
    plus the latency-eligibility conditions.

    `path IS NOT NULL` excludes rows written between migrations 0011 and
    0012, which carry timings but no path - nothing after the fact can tell
    a streamed one from a non-streamed one, so they cannot be assigned to
    either side of the streaming split. This self-heals as those rows age
    out of the reporting window.

    The `outcome` condition excludes failed rows (`provider_error` /
    `client_disconnect`, #17): their `duration_ms` is real (see
    StreamTimer.finish(succeeded=False)), but a percentile blending "how
    long a normal request takes" with "how long a request took before it
    failed" would describe neither quantity. NULL passes through (a
    pre-0013 row, or any row logged without an explicit outcome), matching
    how NULL `path` predates migration 0012.
    """
    return [
        *_base_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name),
        RequestLog.path.isnot(None),
        RequestLog.duration_ms.isnot(None),
        or_(
            RequestLog.outcome.is_(None),
            RequestLog.outcome == "ok",
        ),
    ]


def _percentile_exprs(column, condition) -> list:
    """Return p50/p95/p99 `percentile_cont` expressions over `column`,
    restricted to the rows matching `condition` via an aggregate FILTER.

    `percentile_cont` ignores NULLs in its sorted input, which is what makes
    `provider_ms` percentiles correct without a second filter: a cache hit's
    NULL drops out rather than counting as a zero-length provider call.
    """
    return [
        func.percentile_cont(q).within_group(column.asc()).filter(condition) for q in _QUANTILES
    ]


def _percentiles(p50, p95, p99) -> Percentiles | None:
    """Build a `Percentiles` from three raw aggregate values, or None when
    the ordered set was empty.

    All three come from the same input set, so a non-NULL p50 implies all
    three are non-NULL. Returning None rather than zeros keeps "no data"
    distinguishable from "0 ms" in a cost-only workload.
    """
    if p50 is None:
        return None
    return Percentiles(p50_ms=float(p50), p95_ms=float(p95), p99_ms=float(p99))


async def _latency_breakdown(
    session: AsyncSession, group_col, filters: list, *, condition
) -> list[LatencyBreakdownRow]:
    """Run one GROUP BY latency aggregate over RequestLog for `group_col`.

    `condition` restricts which rows feed the count and the percentiles
    (typically `_NON_STREAMING`, or `true()` for the by-path breakdown,
    which is the one place both sides are shown side by side); `filters`
    (the `path IS NOT NULL` / time-window / model-key-prompt clauses)
    restricts which rows the GROUP BY sees at all. A group with at least one
    row passing `filters` but none passing `condition` - a model queried
    only over its streaming rows, say - still appears, with `sample_count`
    0 and NULL percentiles. A group with no rows passing `filters` at all -
    e.g. a model whose every row predates migration 0012 - is absent from
    the result entirely, so a caller joining against a usage breakdown is
    not guaranteed to find every key there. Ordered by sample count
    descending. NULL group values render as `_NO_PROMPT_LABEL`, matching
    `_breakdown`.
    """
    sample_count = func.count(RequestLog.id).filter(condition)
    rows = (
        await session.execute(
            select(
                group_col,
                sample_count,
                func.percentile_cont(0.5)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
                func.percentile_cont(0.95)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
            )
            .where(*filters)
            .group_by(group_col)
            .order_by(sample_count.desc())
        )
    ).all()
    return [
        LatencyBreakdownRow(
            key=_NO_PROMPT_LABEL if value is None else str(value),
            sample_count=int(count),
            p50_ms=None if p50 is None else float(p50),
            p95_ms=None if p95 is None else float(p95),
        )
        for value, count, p50, p95 in rows
    ]


async def _latency_key_breakdown(
    session: AsyncSession, filters: list, *, condition
) -> list[LatencyBreakdownRow]:
    """Run the same aggregate as `_latency_breakdown` grouped by
    `RequestLog.key_id`, joining `ApiKey` to attach each key's display name.

    Uses an outer join so requests from a since-deleted API key still show
    up, with `label` falling back to `#<id>` - mirroring `_key_breakdown`.
    """
    sample_count = func.count(RequestLog.id).filter(condition)
    rows = (
        await session.execute(
            select(
                RequestLog.key_id,
                ApiKey.name,
                sample_count,
                func.percentile_cont(0.5)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
                func.percentile_cont(0.95)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
            )
            .outerjoin(ApiKey, RequestLog.key_id == ApiKey.id)
            .where(*filters)
            .group_by(RequestLog.key_id, ApiKey.name)
            .order_by(sample_count.desc())
        )
    ).all()
    return [
        LatencyBreakdownRow(
            key=str(key_id),
            label=name if name is not None else f"#{key_id}",
            sample_count=int(count),
            p50_ms=None if p50 is None else float(p50),
            p95_ms=None if p95 is None else float(p95),
        )
        for key_id, name, count, p50, p95 in rows
    ]


class LatencySummaryResponse(BaseModel):
    """Latency percentiles over a time range, plus breakdowns by path,
    model, API key, and prompt name.

    `sample_count` counts every latency-eligible row in the window
    regardless of path, so it reports the true window size; the narrower
    subsets each percentile block is computed over are visible per row in
    `by_path`. `e2e_ms`/`provider_ms`/`overhead_ms` cover the non-streaming
    paths only; `stream_ttlt_ms`/`ttft_ms` cover the streaming one.
    """

    start: datetime
    end: datetime
    sample_count: int
    e2e_ms: Percentiles | None
    provider_ms: Percentiles | None
    overhead_ms: Percentiles | None
    stream_ttlt_ms: Percentiles | None
    ttft_ms: Percentiles | None
    by_path: list[LatencyBreakdownRow]
    by_model: list[LatencyBreakdownRow]
    by_key: list[LatencyBreakdownRow]
    by_prompt: list[LatencyBreakdownRow]


@router.get("/latency/summary", response_model=LatencySummaryResponse)
async def latency_summary(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> LatencySummaryResponse:
    """Return latency percentiles over a time range, broken down by path,
    model, key, and prompt name.

    Same defaults and filters as `usage_summary`: `start`/`end` default to
    the trailing 7 days, and `model`/`key_id`/`prompt_name` are optional
    equality filters. Rows with no `path` (written before migration 0012)
    are excluded throughout. Every percentile block is None for an empty
    set rather than zero. Requires a valid API key (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _latency_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name)
    filters += _account_scope(caller_account)

    row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                *_percentile_exprs(RequestLog.duration_ms, _NON_STREAMING),
                *_percentile_exprs(RequestLog.provider_ms, _NON_STREAMING),
                *_percentile_exprs(_OVERHEAD_MS, _NON_STREAMING),
                *_percentile_exprs(RequestLog.duration_ms, _STREAMING),
                *_percentile_exprs(RequestLog.ttft_ms, _STREAMING),
            ).where(*filters)
        )
    ).one()
    sample_count = int(row[0])
    e2e = _percentiles(*row[1:4])
    provider = _percentiles(*row[4:7])
    overhead = _percentiles(*row[7:10])
    stream_ttlt = _percentiles(*row[10:13])
    ttft = _percentiles(*row[13:16])

    by_path = await _latency_breakdown(session, RequestLog.path, filters, condition=true())
    by_model = await _latency_breakdown(
        session, RequestLog.model, filters, condition=_NON_STREAMING
    )
    by_key = await _latency_key_breakdown(session, filters, condition=_NON_STREAMING)
    by_prompt = await _latency_breakdown(
        session, RequestLog.prompt_name, filters, condition=_NON_STREAMING
    )

    return LatencySummaryResponse(
        start=start,
        end=end,
        sample_count=sample_count,
        e2e_ms=e2e,
        provider_ms=provider,
        overhead_ms=overhead,
        stream_ttlt_ms=stream_ttlt,
        ttft_ms=ttft,
        by_path=by_path,
        by_model=by_model,
        by_key=by_key,
        by_prompt=by_prompt,
    )


class LatencyTimeseriesBucket(BaseModel):
    """One time bucket of latency percentiles, in the flat field style the
    other timeseries buckets use.

    The same streaming split as `LatencySummaryResponse` holds per bucket:
    the `e2e`/`provider`/`overhead` fields cover non-streaming paths and
    `ttft` covers the streaming one. Any field is None when that bucket had
    no qualifying rows.

    Time-to-last-token is deliberately absent: a series whose height tracks
    generation length says more about prompt mix than about gateway
    performance, so it stays a summary-level figure.
    """

    bucket_start: datetime
    sample_count: int
    e2e_p50_ms: float | None
    e2e_p95_ms: float | None
    provider_p50_ms: float | None
    provider_p95_ms: float | None
    overhead_p50_ms: float | None
    overhead_p95_ms: float | None
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None


class LatencyTimeseriesResponse(BaseModel):
    """Latency percentiles bucketed over a time range, for charting."""

    start: datetime
    end: datetime
    interval: str
    buckets: list[LatencyTimeseriesBucket]


def _optional_ms(value) -> float | None:
    """Coerce one raw percentile aggregate to a float, preserving NULL.

    Parameters:
        value: The raw aggregate value from the query result, or None.

    Returns:
        The value as a float, or None if the input was None.
    """
    return None if value is None else float(value)


@router.get("/latency/timeseries", response_model=LatencyTimeseriesResponse)
async def latency_timeseries(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    interval: Literal["minute", "hour", "day"] = Query(default="day"),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    caller_account: Account = Depends(_require_caller_account),
) -> LatencyTimeseriesResponse:
    """Return end-to-end, provider, gateway-overhead, and TTFT percentiles
    bucketed by minute, hour, or day.

    Same filters and defaults as `usage_timeseries`; `interval` selects the
    bucket width via Postgres `date_trunc`. There is deliberately no
    per-path series: `by_path` on the summary answers "how fast is a cache
    hit" without multiplying the response size. Requires a valid API key
    (`require_api_key`).

    Parameters:
        start: Window start; defaults to the trailing 7-day window's start.
        end: Window end; defaults to the trailing 7-day window's end.
        interval: Bucket width, one of "minute", "hour", or "day".
        model: Optional equality filter on `RequestLog.model`.
        key_id: Optional equality filter on `RequestLog.key_id`.
        prompt_name: Optional equality filter on `RequestLog.prompt_name`.
        session: Database session, injected.
        _caller: The authenticated caller, injected; unused beyond auth.

    Returns:
        A `LatencyTimeseriesResponse` with one bucket per distinct
        `date_trunc(interval, created_at)` value in range, ordered
        ascending.
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _latency_filters(start, end, model=model, key_id=key_id, prompt_name=prompt_name)
    filters += _account_scope(caller_account)

    bucket = func.date_trunc(interval, RequestLog.created_at)
    rows = (
        await session.execute(
            select(
                bucket,
                func.count(RequestLog.id),
                func.percentile_cont(0.5)
                .within_group(RequestLog.duration_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.95)
                .within_group(RequestLog.duration_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.5)
                .within_group(RequestLog.provider_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.95)
                .within_group(RequestLog.provider_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.5).within_group(_OVERHEAD_MS.asc()).filter(_NON_STREAMING),
                func.percentile_cont(0.95).within_group(_OVERHEAD_MS.asc()).filter(_NON_STREAMING),
                func.percentile_cont(0.5).within_group(RequestLog.ttft_ms.asc()).filter(_STREAMING),
                func.percentile_cont(0.95)
                .within_group(RequestLog.ttft_ms.asc())
                .filter(_STREAMING),
            )
            .where(*filters)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    buckets = [
        LatencyTimeseriesBucket(
            bucket_start=bucket_start,
            sample_count=int(count),
            e2e_p50_ms=_optional_ms(e2e_p50),
            e2e_p95_ms=_optional_ms(e2e_p95),
            provider_p50_ms=_optional_ms(provider_p50),
            provider_p95_ms=_optional_ms(provider_p95),
            overhead_p50_ms=_optional_ms(overhead_p50),
            overhead_p95_ms=_optional_ms(overhead_p95),
            ttft_p50_ms=_optional_ms(ttft_p50),
            ttft_p95_ms=_optional_ms(ttft_p95),
        )
        for (
            bucket_start,
            count,
            e2e_p50,
            e2e_p95,
            provider_p50,
            provider_p95,
            overhead_p50,
            overhead_p95,
            ttft_p50,
            ttft_p95,
        ) in rows
    ]
    return LatencyTimeseriesResponse(start=start, end=end, interval=interval, buckets=buckets)


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
