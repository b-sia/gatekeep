from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable

from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.config import get_settings
from gatekeep.middleware.ratelimit import get_redis, require_rate_limit
from gatekeep.observability.metrics import budget_alerts_total
from gatekeep.storage.db import get_session
from gatekeep.storage.models import Account, ApiKey, RequestLog

logger = logging.getLogger(__name__)

# How long a period's Redis spend counter and alert markers live for. A
# calendar month is at most 31 days; the extra buffer covers clock skew and
# lets a DB-fallback read that happens right at a period boundary still find
# a freshly-seeded key rather than treating it as a miss.
_PERIOD_TTL_SECONDS = 40 * 24 * 60 * 60


def _current_period(now: dt.datetime | None = None) -> str:
    """Return the current UTC calendar-month period label, e.g. "2026-07".

    `now` is only overridable for tests; defaults to the current UTC time.
    Budgets reset every calendar month by construction: a new period label
    means a fresh Redis counter and a fresh DB aggregation window.
    """
    now = now or dt.datetime.now(dt.UTC)
    return f"{now.year:04d}-{now.month:02d}"


def _period_start(period: str) -> dt.datetime:
    """Return the UTC datetime marking the start of a "YYYY-MM" period label."""
    year, month = (int(p) for p in period.split("-"))
    return dt.datetime(year, month, 1, tzinfo=dt.UTC)


def _spend_redis_key(account_id: int, period: str) -> str:
    """Return the Redis key holding an account's cumulative USD spend for a period."""
    return f"budget:spend:{account_id}:{period}"


def _alert_redis_key(account_id: int, period: str, label: str) -> str:
    """Return the Redis key used as a once-per-period marker for a given alert label."""
    return f"budget:alerted:{account_id}:{period}:{label}"


async def record_spend(
    redis: Redis, *, account_id: int, cost_usd: float, now: dt.datetime | None = None
) -> float:
    """Add `cost_usd` to an account's running Redis spend counter for the period.

    Called from `accounting.log_request` right after a request is persisted,
    so the counter tracks spend as an accelerator for `get_period_spend`
    without a full-table aggregation on every request. Budget is pooled at
    the account, so the counter is keyed by `account_id`: every
    key on the account draws down the same shared quota. Uses INCRBYFLOAT,
    which is atomic per-key in Redis, so concurrent requests for the same
    account can't race each other's increments. Refreshes the TTL on every
    write so an active account's counter doesn't expire mid-month.

    Raises:
        redis.exceptions.RedisError: if Redis is unreachable. Callers should
            treat this as best-effort and not let it fail the request that
            triggered the write. Note this is NOT self-healing on its own:
            `get_period_spend`'s DB fallback only fires when the Redis key
            is missing, not when it's merely stale, so a lost increment
            here under-counts the account until the periodic
            `run_budget_reconciliation_loop` next overwrites the counter
            from the DB aggregate (see issue #27).
    """
    now = now or dt.datetime.now(dt.UTC)
    period = _current_period(now)
    redis_key = _spend_redis_key(account_id, period)
    total = await redis.incrbyfloat(redis_key, cost_usd)
    await redis.expire(redis_key, _PERIOD_TTL_SECONDS)
    return float(total)


async def _aggregate_spend_from_db(
    session: AsyncSession, account_id: int, period_start: dt.datetime
) -> float:
    """Sum non-cached `request_logs.cost_usd` for an account from `period_start` on.

    The safety-net path when the Redis spend counter is missing or
    unreachable: slower (a table scan/index scan per call) but always
    correct, since request_logs is the durable source of truth for cost.

    Excludes cached rows to match `record_spend`'s accounting: a served-from-
    cache response has no upstream provider spend, even though its
    `cost_usd` still records the full notional cost for chargeback/dashboard
    purposes elsewhere.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(RequestLog.cost_usd), 0.0)).where(
            RequestLog.account_id == account_id,
            RequestLog.created_at >= period_start,
            RequestLog.cached.is_(False),
        )
    )
    return float(result.scalar_one())


async def get_period_spend(
    session: AsyncSession | None,
    redis: Redis,
    *,
    account_id: int,
    now: dt.datetime | None = None,
) -> float:
    """Return an account's cumulative USD spend for the current period.

    Reads the Redis counter first (fast path). On a cache miss (account never
    written this period, e.g. right after Redis was flushed) or a Redis
    error, falls back to aggregating `request_logs` directly and seeds
    Redis with the result so subsequent calls hit the fast path again.
    `session` may be omitted only when the caller already knows Redis has
    the value (there is no fallback path to take without it).

    Unlike rate limiting, this never fails closed on a Redis outage: a
    budget check that can't reach its accelerator falls back to a slower
    but still-correct DB read rather than blocking (or wrongly allowing)
    every request for the account.
    """
    now = now or dt.datetime.now(dt.UTC)
    period = _current_period(now)
    redis_key = _spend_redis_key(account_id, period)
    try:
        cached = await redis.get(redis_key)
    except RedisError:
        logger.warning(
            "Budget spend lookup failed (Redis unavailable); falling back to DB aggregate.",
            extra={"account_id": account_id},
        )
        cached = None
        redis = None  # avoid a second failing call below when seeding
    if cached is not None:
        return float(cached)

    if session is None:
        raise ValueError("session is required on a Redis cache miss")
    spent = await _aggregate_spend_from_db(session, account_id, _period_start(period))
    if redis is not None:
        try:
            await redis.set(redis_key, spent, ex=_PERIOD_TTL_SECONDS)
        except RedisError:
            logger.warning(
                "Failed to seed budget spend cache after DB fallback (Redis unavailable).",
                extra={"account_id": account_id},
            )
    return spent


async def _aggregate_spend_from_db_batch(
    session: AsyncSession, account_ids: list[int], period_start: dt.datetime
) -> dict[int, float]:
    """Sum non-cached `request_logs.cost_usd` for several accounts in one query.

    Batched counterpart to `_aggregate_spend_from_db`: a single grouped
    aggregate instead of one query per account. An account with no matching
    rows is simply absent from the result; callers should default it to 0.0.
    """
    result = await session.execute(
        select(RequestLog.account_id, func.coalesce(func.sum(RequestLog.cost_usd), 0.0))
        .where(
            RequestLog.account_id.in_(account_ids),
            RequestLog.created_at >= period_start,
            RequestLog.cached.is_(False),
        )
        .group_by(RequestLog.account_id)
    )
    return {account_id: float(total) for account_id, total in result.all()}


async def get_period_spend_batch(
    session: AsyncSession | None,
    redis: Redis,
    *,
    account_ids: list[int],
    now: dt.datetime | None = None,
) -> dict[int, float]:
    """Return current-period USD spend for several accounts at once.

    Batched counterpart to `get_period_spend`, for callers (the operator
    "all accounts" table) that would otherwise issue one Redis round-trip
    and possibly one DB aggregate per account. Reads every account's Redis
    counter in a single MGET (fast path), then - for whichever accounts
    missed - runs one grouped DB aggregate covering all of them and seeds
    Redis with each result, same fallback behavior as `get_period_spend`.

    `session` may be omitted only when the caller already knows Redis has
    every value (there is no fallback path to take without it).
    """
    if not account_ids:
        return {}
    now = now or dt.datetime.now(dt.UTC)
    period = _current_period(now)
    redis_keys = [_spend_redis_key(account_id, period) for account_id in account_ids]
    try:
        cached = await redis.mget(redis_keys)
    except RedisError:
        logger.warning(
            "Batched budget spend lookup failed (Redis unavailable); falling back to DB aggregate.",
            extra={"account_ids": account_ids},
        )
        cached = [None] * len(account_ids)
        redis = None  # avoid further failing calls below when seeding

    results: dict[int, float] = {}
    missing: list[int] = []
    for account_id, value in zip(account_ids, cached, strict=True):
        if value is not None:
            results[account_id] = float(value)
        else:
            missing.append(account_id)

    if not missing:
        return results

    if session is None:
        raise ValueError("session is required on a Redis cache miss")
    aggregated = await _aggregate_spend_from_db_batch(session, missing, _period_start(period))
    for account_id in missing:
        spent = aggregated.get(account_id, 0.0)
        results[account_id] = spent
        if redis is not None:
            try:
                await redis.set(_spend_redis_key(account_id, period), spent, ex=_PERIOD_TTL_SECONDS)
            except RedisError:
                logger.warning(
                    "Failed to seed budget spend cache after DB fallback (Redis unavailable).",
                    extra={"account_id": account_id},
                )
                redis = None  # subsequent seeds would fail the same way
    return results


async def reconcile_period_spend(
    session: AsyncSession, redis: Redis, *, now: dt.datetime | None = None
) -> dict[int, float]:
    """Recompute every account's Redis spend counter from `request_logs` and overwrite it.

    Unlike `get_period_spend`, this always overwrites the Redis counter with
    the freshly aggregated DB value, even when a key is already present. It
    is the self-heal for the drift `record_spend`'s docstring can't
    actually promise on its own: an increment lost to a transient Redis
    error, a process crash between the DB commit and the Redis call, or
    ordinary `INCRBYFLOAT` float accumulation over a month all leave the
    Redis counter permanently wrong, and `get_period_spend`'s DB fallback
    only fires on a *missing* key, never a stale one (see issue #27).

    Meant to be run periodically (`run_budget_reconciliation_loop`) rather
    than on the request path: it does one grouped DB aggregate covering
    every account per call.

    Returns the freshly computed per-account totals for the current period.
    """
    now = now or dt.datetime.now(dt.UTC)
    period = _current_period(now)
    account_ids = list((await session.execute(select(Account.id))).scalars().all())
    if not account_ids:
        return {}
    totals = await _aggregate_spend_from_db_batch(session, account_ids, _period_start(period))
    for account_id in account_ids:
        spent = totals.get(account_id, 0.0)
        try:
            await redis.set(_spend_redis_key(account_id, period), spent, ex=_PERIOD_TTL_SECONDS)
        except RedisError:
            logger.warning(
                "Budget reconciliation failed to write Redis counter; will retry next cycle.",
                extra={"account_id": account_id},
            )
    return {account_id: totals.get(account_id, 0.0) for account_id in account_ids}


async def run_budget_reconciliation_loop(
    session_factory: Callable[[], AsyncSession], redis: Redis, *, interval_seconds: int
) -> None:
    """Run `reconcile_period_spend` on a fixed interval until the task is cancelled.

    Intended to be launched as a background asyncio task from the app
    lifespan (`gatekeep.app._lifespan`), one per process. Runs a cycle
    immediately on start (so drift accumulated before a deploy is healed
    right away) and then every `interval_seconds`. A single cycle's
    failure (e.g. a DB hiccup) is logged and swallowed rather than killing
    the loop - the next cycle just tries again.
    """
    while True:
        try:
            async with session_factory() as session:
                await reconcile_period_spend(session, redis)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Budget reconciliation cycle failed; will retry next interval.")
        await asyncio.sleep(interval_seconds)


async def _fire_alert_if_new(redis: Redis, account_id: int, period: str, label: str) -> bool:
    """Mark an alert as fired for this account/period/label, True the first time.

    Uses SET NX so concurrent requests for the same account can't both fire
    the same alert; returns False (no-op) on every call after the first one
    per period, and also treats a Redis error as "already fired" (fails toward
    under-alerting, not spamming, since alerting is observability rather
    than an enforcement path).
    """
    try:
        return bool(
            await redis.set(
                _alert_redis_key(account_id, period, label),
                "1",
                nx=True,
                ex=_PERIOD_TTL_SECONDS,
            )
        )
    except RedisError:
        return False


async def _maybe_alert(
    redis: Redis,
    account_id: int,
    period: str,
    spent: float,
    budget: float,
    alert_threshold: float,
) -> None:
    """Fire the "warning" and/or "exceeded" alert hooks if spend just crossed them.

    Each label fires at most once per account per period (see
    `_fire_alert_if_new`). An alert is a structured log line plus a
    Prometheus counter increment - deliberately not a full notification
    system (no email/Slack), per the scoped-down design for this feature.
    """
    if spent >= budget:
        if await _fire_alert_if_new(redis, account_id, period, "exceeded"):
            logger.warning(
                "Budget exceeded for account %s: spent $%.4f of $%.4f budget (period %s)",
                account_id,
                spent,
                budget,
                period,
            )
            budget_alerts_total.labels(threshold="exceeded").inc()
    elif spent >= budget * alert_threshold:
        if await _fire_alert_if_new(redis, account_id, period, "warning"):
            logger.warning(
                "Budget warning for account %s: spent $%.4f of $%.4f budget "
                "(%.0f%% threshold, period %s)",
                account_id,
                spent,
                budget,
                alert_threshold * 100,
                period,
            )
            budget_alerts_total.labels(threshold="warning").inc()


async def check_budget(
    session: AsyncSession,
    redis: Redis,
    account: Account,
    alert_threshold: float | None = None,
    now: dt.datetime | None = None,
) -> tuple[bool, float | None]:
    """Check whether an account is within its monthly budget, firing alert hooks.

    Budget is pooled at the account: every key on the account
    draws down one shared quota. Returns (allowed, spent). `spent` is None
    (and allowed is always True) when `account.monthly_budget_usd` is None
    (unlimited): unlimited accounts skip the spend lookup entirely rather
    than paying for one that can never matter.

    The hard cap check is `spent < budget`: since a request's cost is only
    known after it completes, this only ever blocks a request *after* an
    earlier one pushed spend to/over the cap - not that earlier request
    itself. This matches the token-bucket rate limiter's per-request
    granularity and is an accepted tradeoff rather than a bug.

    Note this tradeoff compounds under concurrency: unlike the rate limiter
    (which atomically reserves a token before a request runs), spend is only
    recorded after a request completes (`accounting.log_request` ->
    `record_spend`), so N concurrent requests for the same key can all read
    the same pre-request spend total and all be allowed through. The
    resulting overshoot is bounded by however many requests for the key are
    in flight at once, not by one request - acceptable here because a budget
    cap is a business control, not a correctness-of-service guarantee, but
    worth knowing if this is ever relied on as a hard ceiling.
    """
    if account.monthly_budget_usd is None:
        return True, None

    now = now or dt.datetime.now(dt.UTC)
    period = _current_period(now)
    spent = await get_period_spend(session, redis, account_id=account.id, now=now)

    threshold = (
        alert_threshold if alert_threshold is not None else get_settings().budget_alert_threshold
    )
    await _maybe_alert(redis, account.id, period, spent, account.monthly_budget_usd, threshold)

    return spent < account.monthly_budget_usd, spent


def _budget_exceeded(budget: float, spent: float) -> HTTPException:
    """Build a 429 HTTPException with an OpenAI-shaped body for a budget-cap rejection.

    Mirrors `ratelimit._too_many_requests`'s shape and status code so clients
    can handle both kinds of throttling the same way, but uses a distinct
    error `type` ("budget_exceeded_error") so the two are still
    distinguishable programmatically. Unlike rate limiting there is no
    natural Retry-After (the cap doesn't refill on its own; it resets at the
    next calendar month), so no such header is set.
    """
    return HTTPException(
        status_code=429,
        detail={
            "error": {
                "message": (f"Monthly budget of ${budget:.2f} exceeded (spent ${spent:.2f})."),
                "type": "budget_exceeded_error",
                "code": None,
            }
        },
    )


async def require_budget(
    key: ApiKey = Depends(require_rate_limit),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    """FastAPI dependency enforcing the account's monthly USD spend cap.

    Chains after `require_rate_limit` (itself chained after `require_api_key`),
    so auth and rate limiting are checked first. Loads the caller's Account
    (the shared budget pool) and raises `HTTPException(429)` once
    the account's current-period spend reaches its `monthly_budget_usd`.
    Accounts with no budget set (None) are unaffected. Returns the ApiKey
    unchanged so downstream handlers keep the same dependency contract.

    Deliberately does not fail closed on a Redis outage the way rate
    limiting does: `check_budget`/`get_period_spend` fall back to a DB
    aggregate instead, since a spend cap is a business control, not a
    correctness-of-service concern - the risk of over-spend during a brief
    outage is far smaller than the risk of blocking all traffic for every
    budgeted account because of it.
    """
    redis = get_redis(get_settings())
    account = await session.get(Account, key.account_id)
    allowed, spent = await check_budget(session, redis, account)
    if not allowed:
        raise _budget_exceeded(account.monthly_budget_usd, spent)
    return key
