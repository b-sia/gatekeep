# Observability Consolidation

## Problem

Gatekeep visualizes its observability data across two disjoint applications:

- **Grafana** (`gatekeep/observability/grafana.json`), provisioned by
  `docker-compose.yml:52-70`, served anonymously on port 3000, reading
  Prometheus. Five panels: cost per model, cache hit rate, avg tokens per
  request, rate limit rejections, cumulative cost savings.
- **The first-party SPA** (`dashboard/`, served at `/dashboard`, backed by
  `gatekeep/api/dashboard.py` over `request_logs`). Usage totals, spend and
  savings, per-model usage, token types, breakdowns by model/key/prompt,
  prompt version history, eval history.

The two overlap on cost, savings, and cache hit rate, and disagree on
nothing except precision. Meanwhile the latency data added by
`0011_request_latency` is visible in **neither**:
`docs/superpowers/specs/2026-07-31-latency-observability-design.md:79-81`
made that an explicit non-goal, deferring the UI so it could be designed
against real recorded data. This spec is that follow-up, plus the decision
about which surface owns what.

## Key finding: the two stores are not redundant, they are differently capable

The overlap makes the split look accidental. It is not. The stores answer
different questions and each is structurally incapable of the other's.

**Prometheus cannot do attribution.** `key_id` is deliberately excluded from
every latency metric (`gatekeep/observability/metrics.py:5-8`), because the
wide bucket set puts the per-key series count at roughly 108,000 against
1,100 without it. That decision is correct and permanent. But the questions
people actually ask about latency *are* attribution questions: which key is
slow, did prompt v7 regress against v6, is the cheap model also the slow one.
Postgres carries `key_id`, `prompt_name`, `prompt_version_num`, and
`routed_from` on every row.

**Postgres computes overhead exactly, and Prometheus cannot.**
`metrics.py:140-142` records that subtracting two histograms is not a valid
operation, which is why `gateway_overhead_seconds` had to exist as a third
histogram written by the middleware. In SQL, `duration_ms - provider_ms` is
exact per row, percentile-able, and sliceable by key. Likewise
`percentile_cont` over raw rows beats a histogram quantile, which interpolates
within buckets.

**Postgres has no failures.** `request_logs` records completed requests only.
429s (`gatekeep/middleware/ratelimit.py:146`), budget rejections, and requests
that die before a model resolves never reach it. Those live only in
Prometheus, and are exactly the signals worth alerting on rather than
browsing.

**The bundled Grafana is a demo, not a product surface.** Anyone running
Gatekeep in earnest already has Prometheus and Grafana and will scrape
`/metrics` into them. Shipping a second dashboard on a second port with
`GF_AUTH_ANONYMOUS_ENABLED: "true"` is a local-development convenience, not
something to build the product's analytics story on.

**Half the SPA is not a dashboard.** Prompt version timelines and eval history
are application UI. Grafana will never host them, so consolidating in that
direction leaves two surfaces regardless.

## Decision

`/dashboard` is the **analytics surface**. `/metrics` is the **integration
surface**. The bundled Grafana is rescoped to an **ops view** over the signals
Postgres structurally lacks, and stops duplicating what the SPA owns.

## Goals

- Surface end-to-end, provider, gateway-overhead, and TTFT latency in the SPA,
  filterable by the existing model/key/prompt/time dimensions.
- Add `request_logs.path` so latency can be sliced the same four ways the
  Prometheus histograms already are.
- Give per-key and per-prompt latency attribution a home, since that is the
  capability that justifies choosing Postgres over Prometheus.
- Rescope `grafana.json` to failure signals and real-time tails, removing
  every panel the SPA now owns.
- Update `README.md` so the division of responsibility is stated rather than
  inferred.

## Non-goals

- **No failure or rejection observability.** Filed as issue #17 (failed and
  aborted streams write no `RequestLog` row at all, losing token accounting
  and budget spend). That is a correctness bug that deserves
  reproduction-first treatment, not a panel added as a side effect. This spec
  is designed so the eventual `outcome` column slots in beside `path` without
  reshaping the table twice.
- **No per-segment decomposition of gateway overhead** (embedding vs. pgvector
  vs. Redis vs. DB commit). Unchanged from the latency spec's position.
- **No rollup or pre-aggregation table.** `percentile_cont` over raw rows is
  the right answer at current volume; the rollup is the documented escape
  hatch, not present work.
- **No `docker-compose.yml` change.** Prometheus and Grafana both stay.
- **No changes to the `usage/*` endpoints.** Their response shapes are stable
  contracts and this spec does not touch them.

## Design

### 1. Schema: `request_logs.path`

Migration `0012_request_log_path.py`. One column, `String(32)`, **nullable**,
carrying exactly the values the Prometheus label carries: `cache_exact`,
`cache_semantic`, `provider`, `stream`.

**Not backfilled.** Any value invented for historical rows would be a guess,
and `gatekeep/models.py:82-85` already establishes the house rule against
reading meaning into a NULL that has two possible causes.

Write path, three touch points:

- `log_request` (`gatekeep/accounting.py:45`) gains `path: str | None = None`.
- `_record_completion` (`gatekeep/app.py:180`) forwards the `path` argument it
  **already takes** and already hands to `mark()`. One line.
- `_sse` (`gatekeep/app.py:893`) and `_messages_sse` (`gatekeep/app.py:811`)
  pass `path="stream"`.

Because `_record_completion` feeds `mark()` and `log_request` from the same
parameter, the column cannot drift from the histogram label.

**All latency queries filter `path IS NOT NULL`.** Rows written between
migrations `0011` and `0012` carry timings but no path, and nothing after the
fact can distinguish a streamed row from a non-streamed one. Excluding them is
the only honest option. It self-heals within the dashboard's default 7-day
window; the transient `sample_count` dip is expected, not a defect.

The same migration adds `Index("ix_request_logs_created_at", "created_at")`.
The existing `(key_id, created_at)` composite (`models.py:94-97`) serves
budget's per-key aggregate but not the dashboard's time-only window scans.

### 2. Backend: two endpoints in `gatekeep/api/dashboard.py`

Both reuse `_base_filters` (`dashboard.py:84`) verbatim, so model, key, and
prompt filtering come free, and both require `require_api_key`. A shared
`Percentiles` model (`p50_ms`, `p95_ms`, `p99_ms`) keeps the shapes flat.

**`GET /dashboard/api/latency/summary`** - params identical to
`usage/summary`.

```
sample_count      int                   # all latency-eligible rows, every path
e2e_ms            Percentiles | null    # non-streaming
provider_ms       Percentiles | null    # non-streaming
overhead_ms       Percentiles | null    # non-streaming
stream_ttlt_ms    Percentiles | null    # streaming: start -> last token
ttft_ms           Percentiles | null    # streaming
by_path           [{path, sample_count, p50_ms, p95_ms}]
by_model          [{key, sample_count, p50_ms, p95_ms}]
by_key            [{key, label, sample_count, p50_ms, p95_ms}]
by_prompt         [{key, sample_count, p50_ms, p95_ms}]
```

**The governing rule: every top-level percentile covers non-streaming paths
only, and streaming is reported separately.** This is the one genuinely
dangerous subtlety in the feature. `duration_ms` holds **two different
quantities** depending on path: end-to-end on the non-streaming paths,
time-to-last-token on the streaming one (`models.py:75-88`). `provider_ms`
splits the same way, holding a single call's duration on the non-streaming
paths and the whole stream's duration on the streaming one, and `overhead_ms`
inherits the split from both. A blended percentile across the two would be
meaningless in every case, so none is offered. `e2e_ms`, `provider_ms`, and
`overhead_ms` cover `cache_exact`, `cache_semantic`, and `provider`;
`stream_ttlt_ms` and `ttft_ms` cover `stream`.

`by_path` is the one place both appear side by side, so its `stream` row is
labeled in the UI as time-to-last-token rather than end-to-end. The same
caveat applies to `by_model`, `by_key`, and `by_prompt`, which aggregate
`duration_ms` across whatever paths that dimension actually used; those
breakdowns are therefore computed over non-streaming rows only, matching the
top-level rule, and the panels say so.

`sample_count` at the top level counts every latency-eligible row regardless
of path, so it reports the true size of the window. The narrower subsets each
percentile block is computed over are visible per row in `by_path`.

`COALESCE(provider_ms, 0)` in `overhead_ms` is deliberate and correct: on a
cache hit no provider call was made, so the entire duration is gatekeep's own
time. This matches the middleware's treatment of the same case
(`gatekeep/observability/latency.py:126-131`).

Every percentile field is nullable, returning `null` rather than `0` for a
window with no qualifying rows. A cost-only workload must read "no data", not
"0 ms".

**`GET /dashboard/api/latency/timeseries`** - same params plus
`interval: Literal["minute", "hour", "day"]`, matching `usage/timeseries`.
Buckets carry flat fields in the established `TimeseriesBucket` style:

```
bucket_start, sample_count,
e2e_p50_ms, e2e_p95_ms,
provider_p50_ms, provider_p95_ms,
overhead_p50_ms, overhead_p95_ms,
ttft_p50_ms, ttft_p95_ms
```

The same rule holds per bucket: the `e2e`, `provider`, and `overhead` fields
are non-streaming, `ttft` is streaming. Time-to-last-token is not charted over
time; it is a summary-level figure, since a series whose height tracks
generation length says more about prompt mix than about gateway performance.

No per-path series over time either. `by_path` on the summary answers "how
fast is a cache hit" without multiplying the response size.

### 3. Frontend: `dashboard/`

**New components:**

- **`LatencyPanel.tsx`** - p50 and p95 lines over time from
  `latency/timeseries`, with a client-side metric toggle (End-to-end /
  Provider / Gateway overhead / TTFT) that re-reads already-fetched data
  without a refetch, the pattern `ModelUsagePanel` established. A compact stat
  strip in the panel header carries p50/p95 end-to-end, p95 TTFT, and median
  overhead.
- **`LatencyByPathPanel.tsx`** - horizontal bars, p50 and p95 per path, each
  labeled with its `sample_count`. This panel is the feature's centre of
  gravity: it shows what a cache hit actually saves in wall-clock time, which
  nothing in either dashboard can show today.

Latency stats live in the panel header rather than as two more cards on
`StatRow`, which already holds five. Seven cards wrap badly, and separating
money stats from speed stats is the better read regardless.

**Modified components:**

- `BreakdownTable.tsx` / `BreakdownPanels.tsx` - a `p95` column, joined
  client-side on the existing `key` field against `latency/summary`'s
  breakdowns. Rows with no latency samples render `-`, never `0ms`. This is
  where per-key and per-prompt latency attribution lands, at the cost of one
  column rather than a new panel.
- `DashboardPage.tsx` - two more entries in the existing `Promise.all`,
  inheriting the `UnauthorizedError` handling and the error banner / Retry
  button with no new error-handling code.
- `api/types.ts`, `api/client.ts` - new response types and two fetchers,
  following the existing docstring conventions.

**Page layout**, changes marked:

Header → Filter bar → Stat row → Usage over time → Model usage → Token type →
Spend/Savings → **Latency (new)** → **Latency by path (new)** → Breakdown
tables (**+ p95 column**) → Prompts → Eval history.

Cost story first, then speed, then attribution.

### 4. Grafana rescope: `gatekeep/observability/grafana.json`

Retitled "Gatekeep - Ops" and rebuilt around signals Postgres cannot serve.

**Removed**, because the SPA owns them and computes them exactly where
`increase()` extrapolates: cost per model, cumulative cost savings, cache hit
rate, avg tokens per request. Panel 1's description, which pointed readers at
`/dashboard/api/usage/summary` for per-key data, goes with it - that
signposting is the README's job now.

**Kept:** rate limit rejections.

**Added:** budget alerts by threshold (`gatekeep_budget_alerts_total`, exposed
today but shown nowhere), request rate by model and path, end-to-end p95 by
path, gateway overhead p95, TTFT p95.

Every surviving panel is either a failure signal that never reaches
`request_logs`, or a real-time tail suitable for alerting.

### 5. Documentation

- `README.md:151` - reframe `/metrics` as the integration surface for an
  existing Prometheus, and the bundled Grafana as an ops view.
- `README.md:187-195` - add latency to what `/dashboard` covers, and remove
  "same data as the Grafana dashboard above", which ceases to be true.
- `README.md:68-69` - align both project-layout rows with the above.
- Drive-by fix: `gatekeep/accounting.py:73` repeats a sentence already present
  at line 68 ("`cache_key` default to a non-cache-hit request."). Delete the
  duplicate.

### 6. Data flow and error handling

All dashboard fetches stay inside the single `Promise.all` in
`DashboardPage.load()`. The two new calls inherit the existing
`UnauthorizedError` → key-entry-screen path and the error banner with Retry,
so no new error-handling code is introduced.

The `LatencyPanel` metric toggle is pure client-side state over
already-fetched data, with no network request on toggle.

Empty windows are a normal state, not an error: endpoints return `null`
percentiles and panels render an explicit empty state.

### 7. Testing

- `tests/test_dashboard.py` - one class per new endpoint. Auth required;
  percentiles asserted to **exact** values against seeded rows, since
  `percentile_cont` over a known set is deterministic and needs no tolerance
  window; path filtering correct; `path IS NULL` rows excluded; an empty
  window returns `null` percentiles rather than zeros or a 500.
- `tests/test_accounting.py` - `log_request` persists `path`.
- `tests/test_latency.py` - `_record_completion` and both SSE generators write
  the `path` value matching the Prometheus label they publish, so the two
  stores cannot diverge.
- Frontend: `npm run build` (type-check plus bundle) is the automated gate,
  matching the bar set by the two preceding dashboard specs; manual
  verification against live seeded data is the acceptance check.

## Risks

- **`percentile_cont` is an ordered-set aggregate and therefore sorts.** Over
  a 30-day window at high request volume this is the scaling ceiling of the
  design. The mitigation is a pre-aggregated rollup table, deliberately
  deferred; the `created_at` index buys the headroom until then.
- **DB `duration_ms` is not the Prometheus end-to-end span.** It excludes JSON
  serialization and the socket write on the non-streaming path
  (`models.py:75-81`), so `/dashboard` reads slightly lower than Grafana for
  identical traffic. Panel copy must state what is measured, or the gap will
  be reported as a bug.
- **The `sample_count` dip** while pre-`0012` rows age out of the window, as
  described in section 1.

## Open questions

None blocking.
