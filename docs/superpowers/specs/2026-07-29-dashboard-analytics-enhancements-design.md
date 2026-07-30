# Dashboard Analytics Enhancements

## Problem

The dashboard rebuilt in `docs/superpowers/specs/2026-07-28-dashboard-redesign-design.md`
covers usage totals, a single cost/request-volume-over-time chart, cost
breakdowns by model/key/prompt, prompts, and eval history. User feedback on
that build asked for four additions:

1. Finer-grained usage-over-time (minute buckets, not just hour/day).
2. A panel showing which models are being used over a given time period
   (inspired by the Anthropic Console's "Usage" page, grouped by model).
3. A graphical breakdown of input tokens vs. output tokens vs. tokens
   served from cache.
4. Total spend and total savings, shown both numerically (stat cards) and
   graphically (a chart over time).

## Key finding: "savings" is already computable

The original design spec listed `"savings" as a distinct metric` under
**Not tracked anywhere**. That turns out to be incorrect for the *data*,
even though no dashboard endpoint exposed it yet. In `gatekeep/accounting.py`
and the cache-hit call sites in `gatekeep/app.py`:

- A cache-hit `RequestLog` row stores the **full notional** `cost_usd` and
  token counts (not `$0`/zero tokens) - `log_request` is always called with
  the real `prompt_tokens`/`completion_tokens` and a `cost_usd` computed
  the same way as a fresh generation.
- `cache_cost_saved_usd.inc(cost_usd)` (a Prometheus counter) increments by
  that same value on every cache hit, confirming `cost_usd` on a cached row
  represents "value delivered," and the *savings* is exactly that value.
- Separately, `record_spend` (the budget-cap tracker) treats a cache hit as
  `$0` real spend - i.e. actual money paid to upstream providers already
  excludes cache-hit cost, just not surfaced as a queryable aggregate.

So, using `RequestLog.cached` as the split:
- **Spend** (actual dollars paid) = `sum(cost_usd) WHERE cached = false`
- **Savings** (notional cost avoided) = `sum(cost_usd) WHERE cached = true`
- **Spend + Savings = existing "Total Cost" stat** (which sums `cost_usd`
  across all rows, unfiltered by `cached` - unchanged, still shown).

No schema change, no migration, no new tracking - just new aggregate
queries filtered by the already-recorded `cached` boolean.

"Cached tokens" (item 3) uses the same definition: `total_tokens` summed
over `cached = true` rows - tokens the gateway didn't have to regenerate.
This is a different concept from provider-side prompt-prefix caching
(e.g. Anthropic's cache-read tokens), which Gatekeep does not track at all
and is out of scope here.

## Goals

- Add a `"minute"` bucket option to the existing usage-over-time chart,
  available only when the 24h range is selected (finer buckets over a
  longer range produce unreadably dense, slow-to-render charts).
- Add a new panel showing per-model usage over time, toggleable between
  three metrics (tokens / requests / cost) without re-fetching, modeled on
  the reference screenshot (`model_breakdown.png`): one stacked bar per
  time bucket, one color per model, with a legend.
- Add a new panel showing input/output/cached tokens as a stacked bar
  chart over time.
- Add a new panel showing spend vs. savings as a stacked bar chart over
  time, plus a 5th stat card ("Total Savings") in the existing stat row.
  "Total Spend" is understood as actual dollars paid to providers
  (excludes cache-hit notional cost); it doesn't get its own stat card
  since it's derivable as Total Cost minus Total Savings, but the reader
  can see it directly on the new Spend/Savings chart's own axis/tooltip.

## Non-goals

- No new database columns or migrations - everything is computed from
  `RequestLog.cost_usd`, `.prompt_tokens`, `.completion_tokens`, and
  `.cached`, which already exist.
- No provider-side prompt-caching (cache-read token) tracking - out of
  scope, would require new upstream-response parsing and a schema change
  before any dashboard work could start.
- No backend-side range/interval validation for the new `"minute"` option -
  the frontend simply won't offer `minute` outside the 24h range, matching
  the existing (also client-only) pattern for `hour`/`day`.
- No changes to the eval history, prompts, or breakdown-table panels -
  this is additive, new panels only.

## Design

### 1. Backend changes (`gatekeep/api/dashboard.py`)

**`usage_timeseries` (modified, additive):**

- `interval: Literal["hour", "day"]` becomes
  `interval: Literal["minute", "hour", "day"]`. Postgres `date_trunc`
  already supports `'minute'` natively - no new query pattern needed.
- `TimeseriesBucket` gains 5 additive fields, computed in the same
  aggregate query as the existing `request_count`/`cache_hit_count`/
  `cost_usd`:
  - `prompt_tokens: int`
  - `completion_tokens: int`
  - `cached_tokens: int` (`sum(total_tokens) WHERE cached = true`)
  - `spend_usd: float` (`sum(cost_usd) WHERE cached = false`)
  - `savings_usd: float` (`sum(cost_usd) WHERE cached = true`)

**`usage_summary` (modified, additive):**

- `UsageSummaryResponse` gains `spend_usd: float` and `savings_usd: float`
  totals (same split, summed over the whole window instead of per bucket),
  so `StatRow`'s new "Total Savings" card doesn't need its own fetch.

**New endpoint `GET /dashboard/api/usage/timeseries/by-model`:**

- Same query params as `usage_timeseries` (`start`, `end`, `interval`,
  `model`, `key_id`, `prompt_name`), same auth (`require_api_key`).
- Query: `GROUP BY date_trunc(interval, created_at), RequestLog.model`.
- Response: flat list of
  `{bucket_start: datetime, model: str, request_count: int, total_tokens: int, cost_usd: float}`.
- The frontend pivots this flat list into per-model series client-side
  (grouping by distinct `model` values found in the response), so the
  backend doesn't need to know the full model set ahead of time and the
  response shape stays simple.

### 2. Frontend changes (`dashboard/`)

**New components:**

- `ModelUsagePanel.tsx` - stacked bar chart from
  `usage/timeseries/by-model`, one color per model, `<Legend />` shown. A
  local `useState<'tokens' | 'requests' | 'cost'>` toggle switches which
  field feeds bar height, re-pivoting the already-fetched data - no
  re-fetch on toggle.
- `TokenTypePanel.tsx` - stacked bar chart over time: input tokens /
  output tokens / cached tokens, from the extended `usage/timeseries`
  response.
- `SpendSavingsPanel.tsx` - stacked bar chart over time: spend / savings,
  from the same extended response.

**Modified components:**

- `StatRow.tsx` - gains a 5th card, "Total Savings" (from
  `UsageSummaryResponse.savings_usd`). Grid becomes 5-wide on large
  screens, wrapping naturally on smaller ones (Tailwind `grid-cols-*`
  responsive classes, consistent with the existing card grid's approach).
- `FilterBar.tsx` - the interval `<select>`'s option list becomes
  conditional on `filters.rangeDays`: `1` → Minute/Hourly/Daily, `7`/`30`
  → Hourly/Daily only. If the user is on Minute and switches to a 7d/30d
  range, `DashboardFilters.interval` resets to `"day"`.
- `DashboardPage.tsx` - adds a 5th parallel fetch
  (`getUsageTimeseriesByModel`) to the existing `Promise.all` in `load()`,
  gaining the same `UnauthorizedError` handling and error-banner/retry
  behavior for free. Renders the 3 new panels between the existing
  "Usage over time" chart and the 3 breakdown tables.

**Page layout (top to bottom, changes marked):**

Header → Filter bar → Stat row (**5 cards**) → Usage-over-time (existing)
→ **Model usage (new)** → **Token type (new)** → **Spend/Savings (new)** →
Breakdown tables (existing) → Prompts (existing) → Eval history
(existing).

### 3. Data flow & error handling

- All 5 fetches stay in one `Promise.all` inside `DashboardPage.load()`;
  the existing `UnauthorizedError` catch, error banner, and Retry button
  (added in the dashboard-redesign branch's final review fix wave) cover
  the new call with no new error-handling code.
- The model-breakdown panel's metric toggle is pure client-side state -
  no network request on toggle.
- No interaction with the existing `allModels`-for-filter-dropdown fetch.

### 4. Testing

- Backend: extend `tests/test_dashboard.py` - assertions for the 5 new
  `TimeseriesBucket` fields and the 2 new `UsageSummaryResponse` fields on
  existing seeded-data tests; a new test class for
  `GET /dashboard/api/usage/timeseries/by-model` (auth-required, correct
  per-(bucket, model) grouping); a test asserting `interval=minute` is
  accepted and buckets correctly.
- Frontend: same bar as the original dashboard build - `npm run build`
  passing (type-check + bundle) is the automated gate; manual verification
  against live seeded data (headless-browser screenshot, as performed at
  the end of the original dashboard build) is the acceptance check, no new
  component-test framework introduced.

## Open questions / risks

- None blocking. Whether this ships as a follow-up commit on the existing
  `sdd/dashboard-redesign` branch/PR #12, or as a new branch layered on
  top once that PR merges, is a process decision to make at
  implementation-planning time, not a design fork.
