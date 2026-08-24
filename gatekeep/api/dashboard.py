from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from anthropic import AsyncAnthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import Integer, case, func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep import account_service, promptjobs
from gatekeep.audit import record_audit_event
from gatekeep.config import get_settings
from gatekeep.curation import curate_cases, list_unreviewed, review_case
from gatekeep.db import SessionLocal, get_session
from gatekeep.evals import add_case, create_suite, get_suite_for_prompt
from gatekeep.middleware.auth import require_api_key
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import (
    Account,
    ApiKey,
    AuditEvent,
    EvalCase,
    EvalRun,
    EvalSuite,
    Prompt,
    PromptVersion,
    RequestLog,
)
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    _get_prompt_row,
    add_prompt_version,
    clear_candidate_version,
    create_prompt,
    rollback_prompt,
    set_candidate_version,
)
from gatekeep.providers.anthropic import AnthropicProvider

router = APIRouter(prefix="/dashboard/api", tags=["dashboard"])

# Prompt/eval routes are operator-only, by design (see issue #28).
#
# `Prompt`, `PromptVersion`, `EvalSuite` and `EvalRun` are fleet-wide rows:
# they carry no `account_id`, prompt names are globally unique, and prompt
# mutation is CLI-only (there is no per-tenant prompt registry, nor an HTTP
# write path). Because there is no tenant to scope them to, the dashboard's
# read views over them (`/evals`, `/prompts`, `/prompts/{name}/versions`)
# would otherwise expose every team's prompt names, authorship, notes, and
# eval trends to any authenticated tenant. Rather than adding per-tenant
# ownership to a deliberately fleet-wide model, these three routes are gated
# to operators via `require_operator`, matching the `/accounts` management
# routes. The usage/latency routes stay per-tenant (`_account_scope`) because
# their underlying rows (`RequestLog`) do carry `account_id`.

_NO_PROMPT_LABEL = "(none)"
_FAILED_OUTCOMES = ("provider_error", "client_disconnect")


async def _require_caller_account(
    caller: ApiKey = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> Account:
    """Resolve the authenticated key's Account, for account-scoped dashboards.

    `account_id` is always derived server-side from the authenticated key,
    never accepted as a client-supplied parameter.

    Raises:
        HTTPException: 401 if the key's `account_id` no longer resolves to an
            Account (e.g. a future account-delete path leaves an orphaned
            key). Without this, callers below (`require_operator`,
            `_account_scope`, `require_account_access`) would immediately hit
            `caller_account.is_operator`/`.id` on `None` and raise an
            unhandled `AttributeError` -> 500.
    """
    account = await session.get(Account, caller.account_id)
    if account is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "API key's account no longer exists.",
                    "type": "authentication_error",
                    "code": None,
                }
            },
        )
    return account


def _account_scope(caller_account: Account) -> list:
    """Return the WHERE clauses restricting a usage query to the caller's account.

    A non-operator account sees only its own rows; an operator
    account sees the whole fleet, so this returns no clause. The scope is
    ANDed onto every query, so a non-operator passing another account's
    `key_id` filter gets an empty result rather than a cross-tenant read.
    """
    if caller_account.is_operator:
        return []
    return [RequestLog.account_id == caller_account.id]


def _get_redis() -> Redis:
    """FastAPI dependency yielding the shared async Redis client.

    Management routes need Redis for month-to-date spend via
    `middleware.budget.get_period_spend`; the analytics routes touch only
    Postgres, so this is scoped to the routes that need it.
    """
    return get_redis(get_settings())


def _get_eval_provider() -> AnthropicProvider:
    """FastAPI dependency yielding the provider used for eval/curation LLM calls.

    Mirrors how the CLI builds its provider (`AnthropicProvider(AsyncAnthropic(...))`).
    Isolated as a dependency so tests can override it with a fake provider via
    `app.dependency_overrides`, keeping eval/curation endpoints and their
    background jobs off the real Anthropic API in the suite.
    """
    settings = get_settings()
    return AnthropicProvider(AsyncAnthropic(api_key=settings.anthropic_api_key))


def _forbidden(message: str) -> HTTPException:
    """Build a 403 HTTPException with an OpenAI-shaped error body.

    Args:
        message: Human-readable explanation of why access was denied.

    Returns:
        An `HTTPException` with status 403 and the OpenAI-shaped error body.
    """
    return HTTPException(
        status_code=403,
        detail={"error": {"message": message, "type": "permission_error", "code": None}},
    )


def _error_body(message: str, err_type: str = "invalid_request_error") -> dict:
    """Build an OpenAI-shaped error detail dict for HTTPException(detail=...).

    Args:
        message: Human-readable explanation of the error.
        err_type: The OpenAI-style error type tag.

    Returns:
        A dict of the shape ``{"error": {"message": ..., "type": ..., "code": None}}``.
    """
    return {"error": {"message": message, "type": err_type, "code": None}}


async def require_operator(
    caller_account: Account = Depends(_require_caller_account),
) -> Account:
    """FastAPI dependency that authorizes only operator accounts.

    Builds on `_require_caller_account`; raises a 403 (OpenAI-shaped body)
    when the caller's account is not an operator.

    Args:
        caller_account: The authenticated caller's account, injected.

    Returns:
        The caller's `Account`, when it is an operator account.

    Raises:
        HTTPException: 403 when `caller_account.is_operator` is False.
    """
    if not caller_account.is_operator:
        raise _forbidden("Operator access required.")
    return caller_account


def _authorize_account_access(caller_account: Account, account_id: int) -> None:
    """Authorize an account-scoped action: operator, or the caller's own account.

    Args:
        caller_account: The authenticated caller's account.
        account_id: The account id the request targets.

    Raises:
        HTTPException: 403 when a non-operator targets a different account.
    """
    if caller_account.is_operator or caller_account.id == account_id:
        return
    raise _forbidden("You can only manage your own account.")


async def require_account_access(
    account_id: int,
    caller_account: Account = Depends(_require_caller_account),
) -> Account:
    """FastAPI dependency that authorizes an account-scoped route.

    Allows the account's own caller, or an operator acting on any account.
    Builds on `_require_caller_account`; `account_id` is taken from the
    route's path parameter of the same name.

    Args:
        account_id: The account id the request targets, injected from the path.
        caller_account: The authenticated caller's account, injected.

    Returns:
        The caller's `Account`.

    Raises:
        HTTPException: 403 when a non-operator targets a different account.
    """
    _authorize_account_access(caller_account, account_id)
    return caller_account


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


class AuditEventOut(BaseModel):
    """One audit-log row for the read-only feed."""

    id: int
    created_at: datetime
    actor_account_id: int | None
    actor_label: str
    action: str
    entity_type: str
    entity_ref: str | None
    version_num: int | None
    result: str
    details: dict


class AuditFeedResponse(BaseModel):
    """A page of audit events, newest first."""

    events: list[AuditEventOut]


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


@router.get("/audit", response_model=AuditFeedResponse)
async def audit_feed(
    entity_type: str | None = Query(default=None),
    entity_ref: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> AuditFeedResponse:
    """Return the audit feed, newest first, filterable by entity/action.

    Fleet-wide and operator only. `entity_type`/`entity_ref`/`action` are
    optional equality filters; `limit` caps the page (default 100, max 500).
    """
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    if entity_type is not None:
        query = query.where(AuditEvent.entity_type == entity_type)
    if entity_ref is not None:
        query = query.where(AuditEvent.entity_ref == entity_ref)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    query = query.limit(limit)

    rows = (await session.execute(query)).scalars().all()
    return AuditFeedResponse(
        events=[
            AuditEventOut(
                id=e.id,
                created_at=e.created_at,
                actor_account_id=e.actor_account_id,
                actor_label=e.actor_label,
                action=e.action,
                entity_type=e.entity_type,
                entity_ref=e.entity_ref,
                version_num=e.version_num,
                result=e.result,
                details=e.details,
            )
            for e in rows
        ]
    )


@router.get("/evals", response_model=EvalHistoryResponse)
async def eval_history(
    prompt_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> EvalHistoryResponse:
    """Return eval run history (score/pass-fail trend), newest first.

    Optionally filtered to a single prompt's suite via `prompt_name`.
    `limit` caps the number of runs returned (default 50, max 500).

    Eval runs are fleet-wide (`EvalSuite`/`EvalRun` carry no `account_id`),
    so this view is gated to operators (`require_operator`) to avoid leaking
    one team's eval trends to another tenant. See the module note on the
    prompt/eval routes.
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
    _operator: Account = Depends(require_operator),
) -> PromptListResponse:
    """List every registered prompt with its active version number, for the
    dashboard's prompt picker.

    Prompts are fleet-wide (`Prompt` carries no `account_id`), so this view
    is gated to operators (`require_operator`). See the module note on the
    prompt/eval routes."""
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
    template: str
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
    _operator: Account = Depends(require_operator),
) -> PromptVersionTimelineResponse:
    """Return `name`'s full version timeline (creation time, author, notes,
    and which version is currently active), ordered oldest-to-newest.

    Raises a 404 if no prompt is registered under `name`. Prompt version
    history (including author identity and free-text notes) is fleet-wide, so
    this view is gated to operators (`require_operator`). See the module note
    on the prompt/eval routes.
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
            template=v.template,
            created_at=v.created_at,
            created_by=v.created_by,
            notes=v.notes,
        )
        for v in rows
    ]
    return PromptVersionTimelineResponse(name=name, versions=versions)


class SuiteOut(BaseModel):
    """An eval suite bound to a prompt."""

    id: int
    name: str
    prompt_name: str
    pass_threshold: float
    created_at: datetime


class EvalCaseOut(BaseModel):
    """One eval case in a suite, reviewed or curated-and-unreviewed."""

    id: int
    check_type: str
    expected: str | None
    judge_criteria: str | None
    reviewed: bool
    source: str
    account_id: int | None
    created_at: datetime
    input_messages: list[dict]


class PromptSuiteResponse(BaseModel):
    """A prompt's eval suite and its cases, or null suite / empty cases."""

    suite: SuiteOut | None
    cases: list[EvalCaseOut]


class CurationResponse(BaseModel):
    """A prompt's unreviewed curated cases."""

    cases: list[EvalCaseOut]


def _case_out(case: EvalCase) -> EvalCaseOut:
    """Map an EvalCase ORM row to its response model."""
    return EvalCaseOut(
        id=case.id,
        check_type=case.check_type,
        expected=case.expected,
        judge_criteria=case.judge_criteria,
        reviewed=case.reviewed,
        source=case.source,
        account_id=case.account_id,
        created_at=case.created_at,
        input_messages=case.input_messages,
    )


class PromptCreateRequest(BaseModel):
    """Request body for creating a prompt (initial version 1, active)."""

    name: str
    template: str
    notes: str | None = None


class PromptVersionCreateRequest(BaseModel):
    """Request body for appending a new inactive version to a prompt."""

    template: str
    notes: str | None = None


class PromptMutationResponse(BaseModel):
    """The prompt name and the version number a mutation produced/left active."""

    name: str
    version_num: int


@router.post("/prompts", response_model=PromptMutationResponse)
async def create_prompt_route(
    body: PromptCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> PromptMutationResponse:
    """Create a prompt with an initial active version 1. Operator only.

    Sets `created_by` to the operator's account name and records a
    `prompt.create` audit event. 400 if the name already exists.
    """
    try:
        prompt = await create_prompt(
            body.name,
            body.template,
            session,
            created_by=operator.name,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.create",
        entity_type="prompt",
        entity_ref=body.name,
        version_num=1,
        result="success",
        details={"notes": body.notes},
    )
    return PromptMutationResponse(name=prompt.name, version_num=1)


@router.post("/prompts/{name}/versions", response_model=PromptMutationResponse)
async def add_prompt_version_route(
    name: str,
    body: PromptVersionCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> PromptMutationResponse:
    """Append a new inactive version to an existing prompt. Operator only.

    Sets `created_by` to the operator's account name and records a
    `prompt.add_version` audit event. 404 if the prompt is unknown.
    """
    try:
        version = await add_prompt_version(
            name, body.template, session, created_by=operator.name, notes=body.notes
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.add_version",
        entity_type="prompt",
        entity_ref=name,
        version_num=version.version_num,
        result="success",
        details={"notes": body.notes},
    )
    return PromptMutationResponse(name=name, version_num=version.version_num)


@router.post("/prompts/{name}/rollback", response_model=PromptMutationResponse)
async def rollback_prompt_route(
    name: str,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(_get_redis),
    operator: Account = Depends(require_operator),
) -> PromptMutationResponse:
    """Revert a prompt to its previously-active version. Operator only.

    Rollback is never eval-gated (reverting to an already-proven version).
    Invalidates the prompt's caches via the service layer and records a
    `prompt.rollback` audit event. 404 if the prompt is unknown, 400 if
    there is no earlier version to roll back to.
    """
    try:
        version = await rollback_prompt(name, session, redis=redis)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.rollback",
        entity_type="prompt",
        entity_ref=name,
        version_num=version.version_num,
        result="success",
        details={"to_version": version.version_num},
    )
    return PromptMutationResponse(name=name, version_num=version.version_num)


class CandidateSetRequest(BaseModel):
    """Request body for setting/adjusting a prompt's A/B candidate."""

    version_num: int
    traffic_pct: float


class CandidateResponse(BaseModel):
    """A prompt's current A/B candidate config (null when none)."""

    name: str
    candidate_version_num: int | None
    traffic_pct: float | None


async def _candidate_response(
    name: str, prompt: Prompt, session: AsyncSession
) -> CandidateResponse:
    """Build a CandidateResponse, resolving candidate_version_id to a version_num."""
    version_num = None
    if prompt.candidate_version_id is not None:
        version = await session.get(PromptVersion, prompt.candidate_version_id)
        version_num = version.version_num if version is not None else None
    return CandidateResponse(
        name=name,
        candidate_version_num=version_num,
        traffic_pct=prompt.candidate_traffic_pct,
    )


@router.put("/prompts/{name}/candidate", response_model=CandidateResponse)
async def set_candidate_route(
    name: str,
    body: CandidateSetRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> CandidateResponse:
    """Configure or adjust a prompt's A/B candidate version + traffic split.

    Never runs the eval gate or invalidates cache (a candidate is not
    "active"). Records a `prompt.set_candidate` audit event. 404 if the
    prompt or version is unknown, 400 if `traffic_pct` is outside [0, 100].
    """
    try:
        prompt = await set_candidate_version(name, body.version_num, body.traffic_pct, session)
    except (PromptNotFoundError, PromptVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.set_candidate",
        entity_type="prompt",
        entity_ref=name,
        version_num=body.version_num,
        result="success",
        details={"traffic_pct": body.traffic_pct},
    )
    return await _candidate_response(name, prompt, session)


@router.delete("/prompts/{name}/candidate", response_model=CandidateResponse)
async def clear_candidate_route(
    name: str,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> CandidateResponse:
    """Clear a prompt's A/B candidate (100% traffic back to active). Operator only.

    Records a `prompt.clear_candidate` audit event. 404 if the prompt is
    unknown. A no-op (but still success) when no candidate was configured.
    """
    try:
        prompt = await clear_candidate_version(name, session)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.clear_candidate",
        entity_type="prompt",
        entity_ref=name,
        result="success",
    )
    return await _candidate_response(name, prompt, session)


@router.get("/prompts/{name}/suite", response_model=PromptSuiteResponse)
async def prompt_suite(
    name: str,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> PromptSuiteResponse:
    """Return the eval suite bound to `name` and its cases.

    Returns `{suite: null, cases: []}` when no suite is registered (not a
    404) - the UI treats "no suite" as an offer to create one. Operator only.
    """
    suite = await get_suite_for_prompt(name, session)
    if suite is None:
        return PromptSuiteResponse(suite=None, cases=[])
    rows = (
        (
            await session.execute(
                select(EvalCase).where(EvalCase.suite_id == suite.id).order_by(EvalCase.id)
            )
        )
        .scalars()
        .all()
    )
    return PromptSuiteResponse(
        suite=SuiteOut(
            id=suite.id,
            name=suite.name,
            prompt_name=suite.prompt_name,
            pass_threshold=suite.pass_threshold,
            created_at=suite.created_at,
        ),
        cases=[_case_out(c) for c in rows],
    )


@router.get("/prompts/{name}/curation", response_model=CurationResponse)
async def prompt_curation(
    name: str,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> CurationResponse:
    """Return `name`'s unreviewed curated eval cases, oldest first. Operator only."""
    cases = await list_unreviewed(name, session)
    return CurationResponse(cases=[_case_out(c) for c in cases])


class SuiteCreateRequest(BaseModel):
    """Request body for creating an eval suite (threshold defaults from settings)."""

    threshold: float | None = None


class CaseCreateRequest(BaseModel):
    """Request body for adding a reviewed manual eval case."""

    input_messages: list[dict]
    check_type: str
    expected: str | None = None
    judge_criteria: str | None = None


class CurationMineRequest(BaseModel):
    """Request body for mining recent samples into unreviewed curated cases."""

    limit: int = 20


class CurationReviewRequest(BaseModel):
    """Request body for approving/rejecting one curated case."""

    approved: bool


@router.post("/prompts/{name}/suite", response_model=SuiteOut)
async def create_suite_route(
    name: str,
    body: SuiteCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> SuiteOut:
    """Create an eval suite for a prompt (one per prompt). Operator only.

    Threshold defaults to `eval_pass_threshold_default`. Records an
    `eval.create_suite` audit event. 400 if a suite already exists.
    """
    threshold = (
        body.threshold if body.threshold is not None else get_settings().eval_pass_threshold_default
    )
    try:
        suite = await create_suite(name, session, pass_threshold=threshold)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=_error_body(f"an eval suite already exists for prompt {name!r}"),
        ) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="eval.create_suite",
        entity_type="eval_suite",
        entity_ref=name,
        result="success",
        details={"threshold": threshold},
    )
    return SuiteOut(
        id=suite.id,
        name=suite.name,
        prompt_name=suite.prompt_name,
        pass_threshold=suite.pass_threshold,
        created_at=suite.created_at,
    )


@router.post("/prompts/{name}/suite/cases", response_model=EvalCaseOut)
async def add_case_route(
    name: str,
    body: CaseCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> EvalCaseOut:
    """Add a reviewed manual eval case to a prompt's suite. Operator only.

    Records an `eval.add_case` audit event. 404 if no suite is registered,
    400 if the check_type/argument combination is invalid.
    """
    suite = await get_suite_for_prompt(name, session)
    if suite is None:
        raise HTTPException(
            status_code=404,
            detail=_error_body(f"no eval suite registered for prompt {name!r}"),
        )
    try:
        case = await add_case(
            suite.id,
            session,
            input_messages=body.input_messages,
            check_type=body.check_type,
            expected=body.expected,
            judge_criteria=body.judge_criteria,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="eval.add_case",
        entity_type="eval_suite",
        entity_ref=name,
        result="success",
        details={"check_type": body.check_type, "case_id": case.id},
    )
    return _case_out(case)


@router.post("/prompts/{name}/curation/mine", response_model=CurationResponse)
async def curation_mine_route(
    name: str,
    body: CurationMineRequest,
    session: AsyncSession = Depends(get_session),
    provider: AnthropicProvider = Depends(_get_eval_provider),
    operator: Account = Depends(require_operator),
) -> CurationResponse:
    """Mine recent request samples for a prompt into unreviewed curated cases.

    Operator only. Uses the eval provider to draft a judge rubric per sample.
    Records a `curation.mine` audit event with the mined case count. 404 if
    no suite is registered.
    """
    try:
        cases = await curate_cases(
            name,
            session,
            limit=body.limit,
            provider=provider,
            generate_model=get_settings().default_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="curation.mine",
        entity_type="prompt",
        entity_ref=name,
        result="success",
        details={"case_count": len(cases)},
    )
    return CurationResponse(cases=[_case_out(c) for c in cases])


class CurationReviewResponse(BaseModel):
    """Terminal state of a reviewed curated case."""

    status: str


@router.post(
    "/prompts/{name}/curation/{case_id}/review",
    response_model=CurationReviewResponse,
)
async def curation_review_route(
    name: str,
    case_id: int,
    body: CurationReviewRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> CurationReviewResponse:
    """Approve (keep, mark reviewed) or reject (delete) one curated case.

    Operator only. Records a `curation.review` audit event. 404 if the case
    id does not exist.
    """
    try:
        await review_case(case_id, session, approve=body.approved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="curation.review",
        entity_type="curated_case",
        entity_ref=name,
        result="success",
        details={"case_id": case_id, "approved": body.approved},
    )
    return CurationReviewResponse(status="reviewed" if body.approved else "rejected")


class MeResponse(BaseModel):
    """The caller's own account context, driving tab visibility and the budget card."""

    account_id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    spend_mtd: float


@router.get("/me", response_model=MeResponse)
async def get_me(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(_get_redis),
    caller_account: Account = Depends(_require_caller_account),
) -> MeResponse:
    """Return the caller's account context: id, name, operator flag, budget
    cap, and current-period budget-relevant spend. Requires a valid API key.

    Args:
        session: Database session, injected.
        redis: Shared async Redis client, injected.
        caller_account: The authenticated caller's account, injected.

    Returns:
        A `MeResponse` describing the caller's account and its month-to-date
        budget-relevant spend.
    """
    spend = await account_service.get_account_spend(session, redis, caller_account.id)
    return MeResponse(
        account_id=caller_account.id,
        name=caller_account.name,
        is_operator=caller_account.is_operator,
        monthly_budget_usd=caller_account.monthly_budget_usd,
        spend_mtd=spend,
    )


class KeyOut(BaseModel):
    """One API key as shown in the management UI (no secret material)."""

    id: int
    name: str
    active: bool
    created_at: datetime


class KeyListResponse(BaseModel):
    """An account's keys, active and revoked, newest first."""

    keys: list[KeyOut]


class KeyCreateRequest(BaseModel):
    """Request body for minting a key: the new key's display name."""

    name: str


class KeyCreatedResponse(BaseModel):
    """A freshly minted key. `key` carries the raw secret exactly once."""

    id: int
    name: str
    active: bool
    created_at: datetime
    key: str


@router.get("/accounts/{account_id}/keys", response_model=KeyListResponse)
async def list_account_keys(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    _caller_account: Account = Depends(require_account_access),
) -> KeyListResponse:
    """List an account's keys. Allowed for the account itself or an operator.

    Raises 403 for a non-operator targeting another account, 404 for an
    unknown account.
    """
    try:
        keys = await account_service.list_keys(session, account_id)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    return KeyListResponse(
        keys=[KeyOut(id=k.id, name=k.name, active=k.active, created_at=k.created_at) for k in keys]
    )


@router.post("/accounts/{account_id}/keys", response_model=KeyCreatedResponse)
async def mint_account_key(
    account_id: int,
    body: KeyCreateRequest,
    session: AsyncSession = Depends(get_session),
    _caller_account: Account = Depends(require_account_access),
) -> KeyCreatedResponse:
    """Mint a key for an account, returning the raw key exactly once.

    Raises 403 (wrong account), 404 (unknown account), 409 (duplicate name).
    """
    try:
        key, raw = await account_service.create_key(session, account_id, body.name)
    except account_service.AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except account_service.KeyNameConflictError as exc:
        raise HTTPException(status_code=409, detail=_error_body(str(exc))) from exc
    return KeyCreatedResponse(
        id=key.id, name=key.name, active=key.active, created_at=key.created_at, key=raw
    )


@router.post("/accounts/{account_id}/keys/{key_id}/revoke", response_model=KeyOut)
async def revoke_account_key(
    account_id: int,
    key_id: int,
    session: AsyncSession = Depends(get_session),
    _caller_account: Account = Depends(require_account_access),
) -> KeyOut:
    """Soft-revoke a key on an account. Raises 403 (wrong account) or 404
    (no such key on the account).
    """
    try:
        key = await account_service.revoke_key(session, account_id, key_id)
    except account_service.KeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    return KeyOut(id=key.id, name=key.name, active=key.active, created_at=key.created_at)


class AccountStatsOut(BaseModel):
    """One account row for the operator's all-accounts table."""

    id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    created_at: datetime
    active_key_count: int
    total_key_count: int
    spend_mtd: float


class AccountListResponse(BaseModel):
    """All accounts with stats, ordered by name (operator view)."""

    accounts: list[AccountStatsOut]


class AccountOut(BaseModel):
    """A single account after a create/patch, without stats."""

    id: int
    name: str
    is_operator: bool
    monthly_budget_usd: float | None
    created_at: datetime


class AccountCreateRequest(BaseModel):
    """Request body for creating an account."""

    name: str
    monthly_budget_usd: float | None = None
    is_operator: bool = False


class AccountPatchRequest(BaseModel):
    """Request body for updating an account.

    Every field is optional so a caller sends only what changes. `clear_budget`
    is a separate flag because `monthly_budget_usd = null` is indistinguishable
    from "field omitted" in JSON, and the two must mean different things
    (clear-the-cap vs. leave-it-alone).
    """

    name: str | None = None
    monthly_budget_usd: float | None = None
    clear_budget: bool = False
    is_operator: bool | None = None


def _account_out(account: Account) -> AccountOut:
    """Map an Account ORM row to the AccountOut response model."""
    return AccountOut(
        id=account.id,
        name=account.name,
        is_operator=account.is_operator,
        monthly_budget_usd=account.monthly_budget_usd,
        created_at=account.created_at,
    )


@router.get("/accounts", response_model=AccountListResponse)
async def list_accounts(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(_get_redis),
    _operator: Account = Depends(require_operator),
) -> AccountListResponse:
    """List all accounts with key counts and month-to-date spend. Operator only."""
    stats = await account_service.list_accounts_with_stats(session, redis)
    return AccountListResponse(
        accounts=[
            AccountStatsOut(
                id=s.id,
                name=s.name,
                is_operator=s.is_operator,
                monthly_budget_usd=s.monthly_budget_usd,
                created_at=s.created_at,
                active_key_count=s.active_key_count,
                total_key_count=s.total_key_count,
                spend_mtd=s.spend_mtd,
            )
            for s in stats
        ]
    )


@router.post("/accounts", response_model=AccountOut)
async def create_account_route(
    body: AccountCreateRequest,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> AccountOut:
    """Create an account. Operator only. 409 on name collision, 422 on bad name/budget."""
    try:
        account = await account_service.create_account(
            session,
            name=body.name,
            monthly_budget_usd=body.monthly_budget_usd,
            is_operator=body.is_operator,
        )
    except (
        account_service.InvalidBudgetError,
        account_service.InvalidAccountNameError,
    ) as exc:
        raise HTTPException(status_code=422, detail=_error_body(str(exc))) from exc
    except account_service.AccountNameConflictError as exc:
        raise HTTPException(status_code=409, detail=_error_body(str(exc))) from exc
    return _account_out(account)


@router.patch("/accounts/{account_id}", response_model=AccountOut)
async def patch_account_route(
    account_id: int,
    body: AccountPatchRequest,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> AccountOut:
    """Rename, set/clear budget, and/or toggle operator on an account.

    Operator only. Applies each requested change through the service layer
    with `commit=False`, so each service guard (last-operator, budget
    validation) still runs, but nothing is written until every requested
    change has succeeded - then this route commits once. That single commit
    makes the whole PATCH atomic: if a later field fails validation (e.g. a
    bad budget after a valid rename), the rename is rolled back too, rather
    than silently persisting while the client sees an error.

    Maps 404 (unknown account), 409 (name collision, last-operator), 422
    (bad name, bad budget).
    """
    try:
        account = None
        if body.name is not None:
            account = await account_service.rename_account(
                session, account_id, body.name, commit=False
            )
        if body.clear_budget:
            account = await account_service.set_budget(session, account_id, None, commit=False)
        elif body.monthly_budget_usd is not None:
            account = await account_service.set_budget(
                session, account_id, body.monthly_budget_usd, commit=False
            )
        if body.is_operator is not None:
            account = await account_service.set_operator(
                session, account_id, body.is_operator, commit=False
            )
        if account is None:
            # No mutating field supplied; return the current state.
            account = await account_service.get_account(session, account_id)
        else:
            await session.commit()
    except account_service.AccountNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except (
        account_service.InvalidBudgetError,
        account_service.InvalidAccountNameError,
    ) as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=_error_body(str(exc))) from exc
    except (
        account_service.AccountNameConflictError,
        account_service.LastOperatorError,
    ) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_error_body(str(exc))) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=_error_body(f"account name {body.name!r} is already taken")
        ) from exc
    return _account_out(account)


class JobProgress(BaseModel):
    """A job's per-case progress counter."""

    done: int
    total: int


class JobResult(BaseModel):
    """A completed eval/promote job's outcome payload."""

    score: float | None = None
    passed: bool | None = None


class JobStatusResponse(BaseModel):
    """Poll response for one background job."""

    id: str
    kind: str
    prompt_name: str
    version_num: int | None
    status: str
    progress: JobProgress
    result: JobResult | None
    error: str | None
    created_at: str
    updated_at: str


class EvalRunRequest(BaseModel):
    """Request body for an on-demand eval run (async job)."""

    version_num: int | None = None
    model: str | None = None


class JobCreatedResponse(BaseModel):
    """The id of a background job the caller should poll."""

    job_id: str


@router.post("/prompts/{name}/eval-run", response_model=JobCreatedResponse)
async def eval_run_route(
    name: str,
    body: EvalRunRequest,
    redis: Redis = Depends(_get_redis),
    provider: AnthropicProvider = Depends(_get_eval_provider),
    operator: Account = Depends(require_operator),
) -> JobCreatedResponse:
    """Kick off an on-demand eval run as a background job. Operator only.

    Returns a job id immediately; the UI polls `GET /prompts/jobs/{id}`. The
    background task drives Redis status, persists the EvalRun, and writes the
    `eval.run` audit event with its outcome (success or error).
    """
    settings = get_settings()
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name=name, version_num=body.version_num
    )
    promptjobs.spawn(
        promptjobs.run_eval_job(
            job_id,
            prompt_name=name,
            version_num=body.version_num,
            model=body.model or settings.default_model,
            provider=provider,
            judge_model=settings.eval_judge_model,
            max_tokens=settings.default_max_tokens,
            actor_account_id=operator.id,
            actor_label=operator.name,
            redis=redis,
            session_factory=SessionLocal,
        )
    )
    return JobCreatedResponse(job_id=job_id)


@router.get("/prompts/jobs/{job_id}", response_model=JobStatusResponse)
async def poll_job(
    job_id: str,
    redis: Redis = Depends(_get_redis),
    _operator: Account = Depends(require_operator),
) -> JobStatusResponse:
    """Return the status of a background job. Operator only.

    404 when the job id is unknown or its TTL has lapsed (the UI renders
    this as "status unavailable, refresh").
    """
    record = await promptjobs.get_job(redis, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=_error_body("job not found or expired"))
    return JobStatusResponse(**record)
