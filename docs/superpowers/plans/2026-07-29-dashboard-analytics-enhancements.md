# Dashboard Analytics Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minute-granularity usage buckets, a per-model usage-over-time panel, an input/output/cached token breakdown chart, and a spend-vs-savings stat card + chart to the existing Gatekeep dashboard, on branch `sdd/dashboard-redesign` (PR #12).

**Architecture:** Additive backend fields on the existing `usage/timeseries` and `usage/summary` endpoints (all derivable from `RequestLog.cost_usd`/`.prompt_tokens`/`.completion_tokens`/`.cached`, no schema change), plus one new endpoint (`usage/timeseries/by-model`) for the model-breakdown panel's 2D (time × model) data. Three new React components consume this data; `DashboardPage.tsx` gains one more parallel fetch.

**Tech Stack:** Same as the existing dashboard - FastAPI/SQLAlchemy backend, React 18 + TypeScript (strict) + Tailwind + Recharts frontend.

## Global Constraints

- No em dashes anywhere (code, comments, commit messages, UI copy).
- All new backend fields are additive only - no existing field renamed/removed/reinterpreted.
- `"minute"` interval is a frontend-only-gated option (only offered when `rangeDays === 1`); the backend accepts it unconditionally for any range, matching the existing (also ungated) `hour`/`day` pattern.
- TypeScript strict mode; no `any` types in new code.
- **Token Type panel uses grouped (not stacked) bars, by design correction:** `cached_tokens` (tokens from cache-hit rows) is *not* mutually exclusive with `prompt_tokens`/`completion_tokens` (a cache-hit row's real token counts are already included in those sums) - stacking all three would double-count. The Spend/Savings panel has no such overlap (`spend_usd` + `savings_usd` = total `cost_usd` exactly, by construction) and stays stacked.
- Frontend component-level automated tests remain out of scope (per the existing dashboard's established convention); `npm run build` passing + manual verification against live seeded data is the acceptance bar.

---

## File Structure

Backend (modified, no new files):
- `gatekeep/api/dashboard.py` - extend `TimeseriesBucket`/`usage_timeseries` (5 new fields, `minute` interval), extend `UsageSummaryResponse`/`usage_summary` (2 new fields), add `UsageByModelBucket`/`UsageByModelTimeseriesResponse`/`usage_timeseries_by_model` (new endpoint).
- `tests/test_dashboard.py` - new assertions and test functions for all of the above.

Frontend (modified + new):
- `dashboard/src/api/types.ts` - extend `TimeseriesBucket`/`UsageSummaryResponse`, add `UsageByModelBucket`/`UsageByModelTimeseriesResponse`.
- `dashboard/src/api/client.ts` - widen `getUsageTimeseries`'s interval type, add `getUsageTimeseriesByModel`.
- `dashboard/src/components/StatRow.tsx` - add "Total savings" 5th card.
- `dashboard/src/components/FilterBar.tsx` - widen `DashboardFilters.interval`, conditional Minute option, auto-reset when leaving the 24h range.
- `dashboard/src/components/ModelUsagePanel.tsx` (new) - per-model stacked bars, tokens/requests/cost toggle.
- `dashboard/src/components/TokenTypePanel.tsx` (new) - input/output/cached grouped bars.
- `dashboard/src/components/SpendSavingsPanel.tsx` (new) - spend/savings stacked bars.
- `dashboard/src/pages/DashboardPage.tsx` - 5th parallel fetch, renders the 3 new panels.

---

### Task 1: Backend - minute interval + token/spend/savings fields on `usage_timeseries`

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `TimeseriesBucket` gains `prompt_tokens: int`, `completion_tokens: int`, `cached_tokens: int`, `spend_usd: float`, `savings_usd: float`. `usage_timeseries`'s `interval` param accepts `"minute"` in addition to `"hour"`/`"day"`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`, in the `-- usage timeseries --` section (after `test_usage_timeseries_buckets_by_day`, before `test_usage_timeseries_rejects_invalid_interval`):

```python
async def test_usage_timeseries_includes_token_and_spend_fields(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=1.0,
        created_at=now,
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=200,
        completion_tokens=100,
        cost_usd=2.0,
        cached=True,
        created_at=now,
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "interval": "day",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["prompt_tokens"] == 300
    assert bucket["completion_tokens"] == 150
    assert bucket["cached_tokens"] == 300
    assert bucket["spend_usd"] == 1.0
    assert bucket["savings_usd"] == 2.0


async def test_usage_timeseries_accepts_minute_interval(client, raw_key, session):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    await _seed_log(
        session, key_id=key_row.id, model="gpt-4o", cost_usd=1.0, created_at=now
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(minutes=5)).isoformat(),
            "end": (now + timedelta(minutes=5)).isoformat(),
            "interval": "minute",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["interval"] == "minute"
    assert sum(b["request_count"] for b in body["buckets"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard.py::test_usage_timeseries_includes_token_and_spend_fields tests/test_dashboard.py::test_usage_timeseries_accepts_minute_interval -v`
Expected: `test_usage_timeseries_includes_token_and_spend_fields` FAILS with `KeyError: 'prompt_tokens'` (field doesn't exist yet). `test_usage_timeseries_accepts_minute_interval` FAILS with a 422 (interval `"minute"` not yet a valid `Literal` value).

- [ ] **Step 3: Implement the backend changes**

In `gatekeep/api/dashboard.py`, change the import line (currently `from sqlalchemy import Integer, func, select`):

```python
from sqlalchemy import Integer, case, func, select
```

Change `TimeseriesBucket` (currently lines 240-246):

```python
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
```

Change the `interval` parameter in `usage_timeseries` (currently line 262):

```python
    interval: Literal["minute", "hour", "day"] = Query(default="day"),
```

Update the docstring line (currently `"""Return request volume, cache-hit count, and cost, bucketed by hour or day.`):

```python
    """Return request volume, cache-hit count, cost, and token/spend/savings
    totals, bucketed by minute, hour, or day.
```

Change the query and bucket-construction block (currently lines 282-309):

```python
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
                    func.sum(
                        case((RequestLog.cached, RequestLog.total_tokens), else_=0)
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case((RequestLog.cached, 0.0), else_=RequestLog.cost_usd)
                    ),
                    0.0,
                ),
                func.coalesce(
                    func.sum(
                        case((RequestLog.cached, RequestLog.cost_usd), else_=0.0)
                    ),
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard.py::test_usage_timeseries_includes_token_and_spend_fields tests/test_dashboard.py::test_usage_timeseries_accepts_minute_interval -v`
Expected: both PASS.

- [ ] **Step 5: Run the full dashboard test module to check for regressions**

Run: `.venv/bin/pytest tests/test_dashboard.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add minute interval and token/spend/savings fields to usage timeseries"
```

---

### Task 2: Backend - spend/savings totals on `usage_summary`

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `UsageSummaryResponse.spend_usd: float`, `UsageSummaryResponse.savings_usd: float`.

- [ ] **Step 1: Write the failing test**

Add these assertions inside `test_usage_summary_totals_and_breakdowns` in `tests/test_dashboard.py`, right after `assert body["completion_tokens"] == 50 + 100 + 5` (currently line 142):

```python
    assert body["spend_usd"] == 1.0 + 0.05
    assert body["savings_usd"] == 2.0
```

(The test seeds 3 logs: `cost_usd=1.0` uncached, `cost_usd=2.0` cached, `cost_usd=0.05` uncached - so spend = the two uncached rows' cost, savings = the one cached row's cost.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns -v`
Expected: FAIL with `KeyError: 'spend_usd'`.

- [ ] **Step 3: Implement the backend changes**

In `gatekeep/api/dashboard.py`, change `UsageSummaryResponse` (currently lines 50-65):

```python
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
    by_model: list[UsageBreakdownRow]
    by_key: list[UsageBreakdownRow]
    by_prompt: list[UsageBreakdownRow]
```

In `usage_summary`, change the totals query (currently lines 193-207):

```python
    totals_row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(
                        case((RequestLog.cached, 0.0), else_=RequestLog.cost_usd)
                    ),
                    0.0,
                ),
                func.coalesce(
                    func.sum(
                        case((RequestLog.cached, RequestLog.cost_usd), else_=0.0)
                    ),
                    0.0,
                ),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
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
    ) = totals_row
    request_count = int(request_count)
    cache_hit_count = int(cache_hit_count)
    cache_hit_rate = (cache_hit_count / request_count) if request_count else 0.0
```

And update the final `return` (currently lines 224-237):

```python
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
        by_model=by_model,
        by_key=by_key,
        by_prompt=by_prompt,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns -v`
Expected: PASS.

- [ ] **Step 5: Run the full dashboard test module to check for regressions**

Run: `.venv/bin/pytest tests/test_dashboard.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add spend/savings totals to usage summary"
```

---

### Task 3: Backend - new `usage/timeseries/by-model` endpoint

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `_default_window`, `_base_filters` (existing helpers).
- Produces: `GET /dashboard/api/usage/timeseries/by-model`, `UsageByModelBucket` (`bucket_start: datetime, model: str, request_count: int, total_tokens: int, cost_usd: float`), `UsageByModelTimeseriesResponse` (`start: datetime, end: datetime, interval: str, rows: list[UsageByModelBucket]`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`, after the `-- usage timeseries --` section's existing tests (after `test_usage_timeseries_accepts_minute_interval`, before the `-- evals --` section):

```python
# -- usage timeseries by model ---------------------------------------------


async def test_usage_timeseries_by_model_requires_auth(client):
    r = await client.get("/dashboard/api/usage/timeseries/by-model")
    assert r.status_code == 401


async def test_usage_timeseries_by_model_groups_by_bucket_and_model(
    client, raw_key, session
):
    key_row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=1.0,
        created_at=now,
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="claude-sonnet-5",
        prompt_tokens=200,
        completion_tokens=100,
        cost_usd=2.0,
        created_at=now,
    )
    await _seed_log(
        session,
        key_id=key_row.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.1,
        created_at=now,
    )

    r = await client.get(
        "/dashboard/api/usage/timeseries/by-model",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "interval": "day",
        },
    )
    assert r.status_code == 200
    body = r.json()
    rows = {row["model"]: row for row in body["rows"]}
    assert rows["gpt-4o"]["request_count"] == 2
    assert rows["gpt-4o"]["total_tokens"] == 165
    assert rows["gpt-4o"]["cost_usd"] == 1.1
    assert rows["claude-sonnet-5"]["request_count"] == 1
    assert rows["claude-sonnet-5"]["total_tokens"] == 300
    assert rows["claude-sonnet-5"]["cost_usd"] == 2.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dashboard.py::test_usage_timeseries_by_model_requires_auth tests/test_dashboard.py::test_usage_timeseries_by_model_groups_by_bucket_and_model -v`
Expected: both FAIL with 404 (route doesn't exist yet).

- [ ] **Step 3: Implement the new endpoint**

In `gatekeep/api/dashboard.py`, add these new models and route directly after the `usage_timeseries` endpoint function (after the line `return TimeseriesResponse(start=start, end=end, interval=interval, buckets=buckets)`, before the `class EvalRunOut(BaseModel):` section):

```python
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


@router.get(
    "/usage/timeseries/by-model", response_model=UsageByModelTimeseriesResponse
)
async def usage_timeseries_by_model(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    interval: Literal["minute", "hour", "day"] = Query(default="day"),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> UsageByModelTimeseriesResponse:
    """Return request volume, tokens, and cost, bucketed by both time and
    model, for the per-model usage-over-time panel.

    Same filters and defaults as `usage_timeseries`. Requires a valid API
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
                RequestLog.model,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
            )
            .where(*filters)
            .group_by(bucket, RequestLog.model)
            .order_by(bucket)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dashboard.py::test_usage_timeseries_by_model_requires_auth tests/test_dashboard.py::test_usage_timeseries_by_model_groups_by_bucket_and_model -v`
Expected: both PASS.

- [ ] **Step 5: Run the full dashboard test module to check for regressions**

Run: `.venv/bin/pytest tests/test_dashboard.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add usage timeseries by-model endpoint"
```

---

### Task 4: Frontend - extend API types and client

**Files:**
- Modify: `dashboard/src/api/types.ts`
- Modify: `dashboard/src/api/client.ts`

**Interfaces:**
- Consumes: nothing new (matches Task 1-3's backend response shapes).
- Produces: `TimeseriesBucket` gains 5 fields; `UsageSummaryResponse` gains 2 fields; new types `UsageByModelBucket`, `UsageByModelTimeseriesResponse`; `getUsageTimeseries`'s `interval` type widens to `"minute" | "hour" | "day"`; new function `getUsageTimeseriesByModel`. Tasks 5-10 import from these.

- [ ] **Step 1: Update `dashboard/src/api/types.ts`**

Change `UsageSummaryResponse` (currently lines 13-26):

```typescript
/** Aggregate usage/cost totals for a time window, plus breakdowns by model,
 * API key, and prompt. */
export interface UsageSummaryResponse {
  start: string;
  end: string;
  request_count: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  spend_usd: number;
  savings_usd: number;
  cache_hit_count: number;
  cache_hit_rate: number;
  by_model: UsageBreakdownRow[];
  by_key: UsageBreakdownRow[];
  by_prompt: UsageBreakdownRow[];
}
```

Change `TimeseriesBucket` (currently lines 30-35):

```typescript
/** One bucket of a usage timeseries (requests/cache hits/cost/tokens within
 * a single minute, hour, or day interval). */
export interface TimeseriesBucket {
  bucket_start: string;
  request_count: number;
  cache_hit_count: number;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  spend_usd: number;
  savings_usd: number;
}
```

Add these two new interfaces directly after the existing `TimeseriesResponse` interface (after line 43, `export interface TimeseriesResponse { ... }`):

```typescript
/** One (time bucket, model) row of request/token/cost totals. */
export interface UsageByModelBucket {
  bucket_start: string;
  model: string;
  request_count: number;
  total_tokens: number;
  cost_usd: number;
}

/** Usage bucketed by both time and model, as a flat list of rows - group by
 * `model` client-side to build per-model chart series. */
export interface UsageByModelTimeseriesResponse {
  start: string;
  end: string;
  interval: string;
  rows: UsageByModelBucket[];
}
```

- [ ] **Step 2: Update `dashboard/src/api/client.ts`**

Change the import line (currently lines 1-7):

```typescript
import type {
  EvalHistoryResponse,
  PromptListResponse,
  PromptVersionTimelineResponse,
  TimeseriesResponse,
  UsageByModelTimeseriesResponse,
  UsageSummaryResponse,
} from "./types";
```

Change `getUsageTimeseries` (currently lines 91-104):

```typescript
/** Fetches usage bucketed into minute, hourly, or daily intervals for the
 * given filters, for charting over time. */
export function getUsageTimeseries(
  filters: UsageFilters & { interval: "minute" | "hour" | "day" },
): Promise<TimeseriesResponse> {
  return request<TimeseriesResponse>("usage/timeseries", {
    start: filters.start,
    end: filters.end,
    interval: filters.interval,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

/** Fetches usage bucketed by both time and model, for the per-model usage
 * panel. */
export function getUsageTimeseriesByModel(
  filters: UsageFilters & { interval: "minute" | "hour" | "day" },
): Promise<UsageByModelTimeseriesResponse> {
  return request<UsageByModelTimeseriesResponse>("usage/timeseries/by-model", {
    start: filters.start,
    end: filters.end,
    interval: filters.interval,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0 (nothing imports the new fields/function yet, but `tsc` checks these files regardless).

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/api/types.ts dashboard/src/api/client.ts
git commit -m "feat(dashboard): add types and client function for by-model timeseries and spend/savings fields"
```

---

### Task 5: Frontend - "Total savings" stat card

**Files:**
- Modify: `dashboard/src/components/StatRow.tsx`

**Interfaces:**
- Consumes: `UsageSummaryResponse.savings_usd` (Task 4).
- Produces: `StatRow` renders 5 cards instead of 4. No other component's interface changes.

- [ ] **Step 1: Update `dashboard/src/components/StatRow.tsx`**

Change the loading-state block (currently lines 35-43):

```tsx
  if (!summary) {
    return (
      <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-5">
        {["Requests", "Total cost", "Total tokens", "Total savings", "Cache hit rate"].map(
          (label) => (
            <StatCard key={label} label={label} value="-" context="Loading..." />
          ),
        )}
      </div>
    );
  }
```

Change the populated-state block (currently lines 45-64):

```tsx
  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-5">
      <StatCard
        label="Requests"
        value={summary.request_count.toLocaleString()}
        context={`${summary.cache_hit_count} cache hits`}
      />
      <StatCard label="Total cost" value={formatCost(summary.cost_usd)} context="Across all models" />
      <StatCard
        label="Total tokens"
        value={formatTokens(summary.total_tokens)}
        context={`${formatTokens(summary.prompt_tokens)} in / ${formatTokens(summary.completion_tokens)} out`}
      />
      <StatCard
        label="Total savings"
        value={formatCost(summary.savings_usd)}
        context={`${formatCost(summary.spend_usd)} spent`}
      />
      <StatCard
        label="Cache hit rate"
        value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`}
        context="Of total requests"
      />
    </div>
  );
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/StatRow.tsx
git commit -m "feat(dashboard): add total savings stat card"
```

---

### Task 6: Frontend - minute interval in FilterBar

**Files:**
- Modify: `dashboard/src/components/FilterBar.tsx`

**Interfaces:**
- Produces: `DashboardFilters.interval` widens to `"minute" | "hour" | "day"`. Task 10 (`DashboardPage.tsx`) already passes `filters.interval` straight through to `getUsageTimeseries`/`getUsageTimeseriesByModel` (Task 4's widened signatures), so no change needed there beyond what Task 10 already does.

- [ ] **Step 1: Update `dashboard/src/components/FilterBar.tsx`**

Replace the full file contents:

```tsx
/** The current dashboard-wide filter selection: time range, chart bucket
 * interval, and an optional model filter. */
export interface DashboardFilters {
  rangeDays: 1 | 7 | 30;
  interval: "minute" | "hour" | "day";
  model: string | null;
}

interface FilterBarProps {
  filters: DashboardFilters;
  availableModels: string[];
  onChange: (filters: DashboardFilters) => void;
}

/** Row of dropdowns for selecting the dashboard's time range, chart
 * interval, and model filter. The Minute interval option is only offered
 * when the 24h range is selected, to keep bucket counts bounded; switching
 * away from 24h while on Minute resets the interval to Daily. */
export default function FilterBar({ filters, availableModels, onChange }: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-slate-800 px-6 py-3">
      <select
        value={filters.rangeDays}
        onChange={(event) => {
          const rangeDays = Number(event.target.value) as 1 | 7 | 30;
          const interval =
            rangeDays === 1 || filters.interval !== "minute" ? filters.interval : "day";
          onChange({ ...filters, rangeDays, interval });
        }}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
      >
        <option value={1}>Last 24h</option>
        <option value={7}>Last 7d</option>
        <option value={30}>Last 30d</option>
      </select>
      <select
        value={filters.interval}
        onChange={(event) =>
          onChange({ ...filters, interval: event.target.value as "minute" | "hour" | "day" })
        }
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
      >
        {filters.rangeDays === 1 && <option value="minute">Minute</option>}
        <option value="hour">Hourly</option>
        <option value="day">Daily</option>
      </select>
      <select
        value={filters.model ?? ""}
        onChange={(event) => onChange({ ...filters, model: event.target.value || null })}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
      >
        <option value="">All models</option>
        {availableModels.map((model) => (
          <option key={model} value={model}>
            {model}
          </option>
        ))}
      </select>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Manual check**

Run: `cd dashboard && npm run dev`. With a running backend, confirm: selecting "Last 24h" shows a Minute option in the interval dropdown; selecting Minute then switching to "Last 7d" or "Last 30d" auto-resets the interval to Daily and hides the Minute option.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/components/FilterBar.tsx
git commit -m "feat(dashboard): add minute interval option gated to the 24h range"
```

---

### Task 7: Frontend - ModelUsagePanel component

**Files:**
- Create: `dashboard/src/components/ModelUsagePanel.tsx`

**Interfaces:**
- Consumes: `UsageByModelTimeseriesResponse` from `../api/types` (Task 4).
- Produces: `ModelUsagePanel` component with props `{ data: UsageByModelTimeseriesResponse | null; interval: "minute" | "hour" | "day" }`. Task 10 renders this in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/ModelUsagePanel.tsx`**

```tsx
import { useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { UsageByModelTimeseriesResponse } from "../api/types";

type Metric = "tokens" | "requests" | "cost";

interface ModelUsagePanelProps {
  data: UsageByModelTimeseriesResponse | null;
  interval: "minute" | "hour" | "day";
}

type ChartRow = Record<string, string | number>;

const METRIC_LABELS: Record<Metric, string> = {
  tokens: "Tokens",
  requests: "Requests",
  cost: "Cost (USD)",
};

const MODEL_COLORS = ["#6366f1", "#f97316", "#22d3ee", "#a3e635", "#f472b6", "#facc15"];

function formatBucketLabel(isoString: string, interval: "minute" | "hour" | "day"): string {
  return new Date(isoString).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: interval !== "day" ? "numeric" : undefined,
    minute: interval === "minute" ? "numeric" : undefined,
  });
}

/** Stacked bar chart of per-model usage over time. A metric toggle switches
 * which field feeds bar height (tokens / requests / cost) by re-pivoting
 * the already-fetched data client-side, without a re-fetch. */
export default function ModelUsagePanel({ data, interval }: ModelUsagePanelProps) {
  const [metric, setMetric] = useState<Metric>("tokens");

  const models = Array.from(new Set((data?.rows ?? []).map((row) => row.model))).sort();

  const byBucket = new Map<string, ChartRow>();
  for (const row of data?.rows ?? []) {
    if (!byBucket.has(row.bucket_start)) {
      const initial: ChartRow = { time: formatBucketLabel(row.bucket_start, interval) };
      for (const modelName of models) initial[modelName] = 0;
      byBucket.set(row.bucket_start, initial);
    }
    const existing = byBucket.get(row.bucket_start)!;
    const value =
      metric === "tokens" ? row.total_tokens : metric === "requests" ? row.request_count : row.cost_usd;
    existing[row.model] = (Number(existing[row.model]) || 0) + value;
  }
  const chartData = Array.from(byBucket.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([, row]) => row);

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium text-slate-300">Model usage</h2>
        <div className="flex gap-1">
          {(Object.keys(METRIC_LABELS) as Metric[]).map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className={`rounded px-2 py-1 text-xs ${
                metric === m ? "bg-indigo-600 text-white" : "text-slate-400 hover:bg-slate-800"
              }`}
            >
              {METRIC_LABELS[m]}
            </button>
          ))}
        </div>
      </div>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {models.map((modelName, i) => (
              <Bar
                key={modelName}
                dataKey={modelName}
                stackId="models"
                fill={MODEL_COLORS[i % MODEL_COLORS.length]}
                name={modelName}
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/ModelUsagePanel.tsx
git commit -m "feat(dashboard): add per-model usage-over-time panel"
```

---

### Task 8: Frontend - TokenTypePanel component

**Files:**
- Create: `dashboard/src/components/TokenTypePanel.tsx`

**Interfaces:**
- Consumes: `TimeseriesResponse` from `../api/types` (Task 4's extended fields).
- Produces: `TokenTypePanel` component with props `{ timeseries: TimeseriesResponse | null }`. Task 10 renders this in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/TokenTypePanel.tsx`**

```tsx
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimeseriesResponse } from "../api/types";

interface TokenTypePanelProps {
  timeseries: TimeseriesResponse | null;
}

/** Grouped (not stacked) bar chart of input/output/cached tokens over time.
 *
 * Bars are grouped rather than stacked because `cached_tokens` is not
 * mutually exclusive with `prompt_tokens`/`completion_tokens`: a cache-hit
 * request's real token counts are already included in those two sums, so
 * stacking all three would double-count. Grouping avoids implying the three
 * bars sum to a combined total.
 */
export default function TokenTypePanel({ timeseries }: TokenTypePanelProps) {
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: new Date(bucket.bucket_start).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: timeseries.interval !== "day" ? "numeric" : undefined,
        minute: timeseries.interval === "minute" ? "numeric" : undefined,
      }),
      input: bucket.prompt_tokens,
      output: bucket.completion_tokens,
      cached: bucket.cached_tokens,
    })) ?? [];

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Token usage</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="input" fill="#6366f1" name="Input tokens" />
            <Bar dataKey="output" fill="#f97316" name="Output tokens" />
            <Bar dataKey="cached" fill="#22d3ee" name="Cached tokens" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/TokenTypePanel.tsx
git commit -m "feat(dashboard): add input/output/cached token breakdown panel"
```

---

### Task 9: Frontend - SpendSavingsPanel component

**Files:**
- Create: `dashboard/src/components/SpendSavingsPanel.tsx`

**Interfaces:**
- Consumes: `TimeseriesResponse` from `../api/types` (Task 4's extended fields).
- Produces: `SpendSavingsPanel` component with props `{ timeseries: TimeseriesResponse | null }`. Task 10 renders this in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/SpendSavingsPanel.tsx`**

```tsx
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimeseriesResponse } from "../api/types";

interface SpendSavingsPanelProps {
  timeseries: TimeseriesResponse | null;
}

/** Stacked bar chart of actual spend vs. cache savings over time. Spend and
 * savings are mutually exclusive per bucket (split by the `cached` flag),
 * so stacking them sums to that bucket's total cost. */
export default function SpendSavingsPanel({ timeseries }: SpendSavingsPanelProps) {
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: new Date(bucket.bucket_start).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: timeseries.interval !== "day" ? "numeric" : undefined,
        minute: timeseries.interval === "minute" ? "numeric" : undefined,
      }),
      spend: bucket.spend_usd,
      savings: bucket.savings_usd,
    })) ?? [];

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Spend vs. savings</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis
              tick={{ fill: "#94a3b8", fontSize: 12 }}
              tickFormatter={(value: number) => `$${value.toFixed(2)}`}
            />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
              formatter={(value: number) => `$${value.toFixed(2)}`}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="spend" stackId="cost" fill="#f97316" name="Spend" />
            <Bar dataKey="savings" stackId="cost" fill="#22d3ee" name="Savings" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/SpendSavingsPanel.tsx
git commit -m "feat(dashboard): add spend vs savings panel"
```

---

### Task 10: Frontend - wire the 3 new panels into DashboardPage

**Files:**
- Modify: `dashboard/src/pages/DashboardPage.tsx`

**Interfaces:**
- Consumes: `ModelUsagePanel` (Task 7), `TokenTypePanel` (Task 8), `SpendSavingsPanel` (Task 9), `getUsageTimeseriesByModel` (Task 4), `UsageByModelTimeseriesResponse` (Task 4).
- Produces: nothing further consumed by later tasks - this is the final integration point.

- [ ] **Step 1: Update `dashboard/src/pages/DashboardPage.tsx`**

Replace the full file contents:

```tsx
import { useCallback, useEffect, useState } from "react";
import Header from "../components/Header";
import FilterBar, { type DashboardFilters } from "../components/FilterBar";
import StatRow from "../components/StatRow";
import UsageChart from "../components/UsageChart";
import ModelUsagePanel from "../components/ModelUsagePanel";
import TokenTypePanel from "../components/TokenTypePanel";
import SpendSavingsPanel from "../components/SpendSavingsPanel";
import BreakdownPanels from "../components/BreakdownPanels";
import PromptsPanel from "../components/PromptsPanel";
import EvalHistoryPanel from "../components/EvalHistoryPanel";
import {
  UnauthorizedError,
  getEvalHistory,
  getPrompts,
  getUsageSummary,
  getUsageTimeseries,
  getUsageTimeseriesByModel,
} from "../api/client";
import type {
  EvalRunOut,
  PromptOut,
  TimeseriesResponse,
  UsageByModelTimeseriesResponse,
  UsageSummaryResponse,
} from "../api/types";

interface DashboardPageProps {
  /** Called when any dashboard API call comes back 401, so the app can drop
   * back to the API key entry screen and clear the stale stored key. */
  onUnauthorized: () => void;
}

/**
 * Top-level dashboard view: owns filter state, fetches usage/eval/prompt
 * data for the current time window and model filter, and renders the
 * dashboard layout (header, filters, stat cards, charts, breakdowns,
 * prompts, eval history).
 */
export default function DashboardPage({ onUnauthorized }: DashboardPageProps) {
  const [filters, setFilters] = useState<DashboardFilters>({
    rangeDays: 7,
    interval: "day",
    model: null,
  });
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [allModels, setAllModels] = useState<string[]>([]);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [byModel, setByModel] = useState<UsageByModelTimeseriesResponse | null>(null);
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [prompts, setPrompts] = useState<PromptOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const end = new Date();
    const start = new Date(end.getTime() - filters.rangeDays * 24 * 60 * 60 * 1000);
    const windowParams = { start: start.toISOString(), end: end.toISOString() };
    try {
      const [summaryRes, timeseriesRes, byModelRes, evalsRes, promptsRes] = await Promise.all([
        getUsageSummary({ ...windowParams, model: filters.model ?? undefined }),
        getUsageTimeseries({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
        getUsageTimeseriesByModel({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
        getEvalHistory(),
        getPrompts(),
      ]);
      setSummary(summaryRes);
      setTimeseries(timeseriesRes);
      setByModel(byModelRes);
      setRuns(evalsRes.runs);
      setPrompts(promptsRes.prompts);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
    }
  }, [filters, onUnauthorized]);

  // Fetch the model list from an *unfiltered* summary (no `model` param) so
  // the dropdown always lists every model seen in the current time window,
  // independent of whichever model is currently selected. Re-fetched only
  // when the time window changes, not on every model-filter change.
  const loadAllModels = useCallback(async () => {
    const end = new Date();
    const start = new Date(end.getTime() - filters.rangeDays * 24 * 60 * 60 * 1000);
    try {
      const res = await getUsageSummary({ start: start.toISOString(), end: end.toISOString() });
      setAllModels(res.by_model.map((row) => row.key));
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      // Non-fatal: the model dropdown just stays stale/empty until the next
      // successful window change. The main `load()` error banner already
      // covers the general "gateway is unreachable" case.
    }
  }, [filters.rangeDays, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadAllModels();
  }, [loadAllModels]);

  return (
    <div className="min-h-screen bg-slate-950">
      <Header onClearKey={onUnauthorized} />
      <FilterBar filters={filters} availableModels={allModels} onChange={setFilters} />
      {error && (
        <div className="mx-6 mt-4 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-300">
          <span>{error}</span>
          <button
            onClick={() => load()}
            className="rounded border border-red-800 px-3 py-1 text-xs text-red-200 hover:bg-red-900"
          >
            Retry
          </button>
        </div>
      )}
      <StatRow summary={summary} />
      <UsageChart timeseries={timeseries} />
      <ModelUsagePanel data={byModel} interval={filters.interval} />
      <TokenTypePanel timeseries={timeseries} />
      <SpendSavingsPanel timeseries={timeseries} />
      <BreakdownPanels summary={summary} />
      <PromptsPanel prompts={prompts} onUnauthorized={onUnauthorized} />
      <EvalHistoryPanel runs={runs} />
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/pages/DashboardPage.tsx
git commit -m "feat(dashboard): wire model usage, token type, and spend/savings panels into dashboard page"
```

---

### Task 11: Full-stack verification

**Files:** none (verification only).

**Interfaces:** none.

- [ ] **Step 1: Run the full backend test suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 2: Bring up the stack and seed data**

Run: `docker compose up -d --build gateway` (postgres/redis should already be running from prior work; rebuild just the gateway to pick up the backend changes). Create/reuse an API key and send a mix of cached and uncached requests across at least 2 different models through `/v1/chat/completions`, so all 3 new panels and the new stat card have real data to render.

- [ ] **Step 3: Manual visual verification**

Open `http://localhost:8100/dashboard` (or use a headless-browser screenshot as done for the original dashboard build, if interactive browser access isn't available). Verify:
- Filter bar's interval dropdown shows "Minute" only when "Last 24h" is selected, and switching to 7d/30d while on Minute resets to Daily.
- Stat row now shows 5 cards, with "Total savings" showing a dollar figure and a "$X spent" context line.
- "Model usage" panel renders a stacked bar chart with one color per model and a legend; clicking Tokens/Requests/Cost changes the bar heights without a network request (check browser devtools network tab, or just confirm the switch feels instant).
- "Token usage" panel renders grouped (not stacked) bars for Input/Output/Cached tokens.
- "Spend vs. savings" panel renders stacked bars for Spend/Savings with dollar-formatted axis/tooltip.
- All three new panels sit between the existing "Usage over time" chart and the "Cost by model/key/prompt" breakdown tables.

- [ ] **Step 4: Fix anything that looks off**

If any visual or functional issue surfaces, fix it before considering this task done, consistent with the project's pixel-perfect UI standard. No separate commit needed for this task unless a fix is required, in which case commit it as its own small commit.

---

## Self-Review Notes

- **Spec coverage:** minute granularity (Task 1, 6), model breakdown panel with tokens/requests/cost toggle (Task 3, 7), input/output/cached token chart (Task 8), spend/savings numerically (Task 2, 5) and graphically (Task 9) - all four feedback items and the spec's design section are covered.
- **Placeholder scan:** no TBD/TODO; every code step has complete code.
- **Type consistency:** `UsageByModelBucket`/`UsageByModelTimeseriesResponse` defined once in `types.ts` (Task 4), used identically in `client.ts` (Task 4), `ModelUsagePanel.tsx` (Task 7), and `DashboardPage.tsx` (Task 10). `DashboardFilters.interval` widened once in `FilterBar.tsx` (Task 6) and consumed as the same type everywhere else.
- **Double-counting correction:** flagged explicitly in Global Constraints and Task 8's component docstring - the Token Type panel intentionally uses grouped, not stacked, bars, since `cached_tokens` overlaps with `prompt_tokens`/`completion_tokens` rather than being mutually exclusive with them (unlike Spend/Savings, which are mutually exclusive by construction).
