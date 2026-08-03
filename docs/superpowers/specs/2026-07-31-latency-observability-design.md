# Latency Observability

## Problem

Gatekeep has extensive observability for *what a request cost* and *how many
tokens it used*, across both Prometheus (`gatekeep/observability/metrics.py`)
and Postgres (`RequestLog`, surfaced by `gatekeep/api/dashboard.py`). It has
**no timing instrumentation of any kind**. There is not one metric, column, or
log field recording how long anything takes.

This is a gap for three separate reasons:

1. **Nobody can see gatekeep's own cost in time.** The gateway does real work
   before it ever calls a provider, and all of it is invisible.
2. **Models can't be compared on speed.** `route_by_cost` trades cost against
   eval quality with no notion that a cheaper model might also be slower (or
   faster).
3. **There is no way to detect latency regression** from a prompt version, a
   routing change, or an infrastructure change.

## Key finding: gateway overhead here is not negligible

On the non-streaming path, `chat_completions` does the following *before* the
provider call (`gatekeep/app.py`):

| Step | Cost |
|---|---|
| `require_budget` / rate limit (FastAPI dependencies) | Redis round trips |
| `resolve_prompt_version_for_request` | DB query |
| `get_cached_response` (app.py:314) | Redis |
| `embed_text` (app.py:349) | **local embedding inference, synchronous** |
| `find_semantic_match` (app.py:351) | pgvector similarity scan |

and *after* the provider returns, still before the client receives bytes:
`set_cached_response`, `store_cached_response`, `record_request_sample`, and
the `log_request` commit.

`embed_text` plus the pgvector scan runs on **every cache miss**, which is the
common case. If gatekeep has a latency story worth telling, it is most likely
there. Today it is entirely unmeasured.

## Key finding: the four classic metrics do not apply uniformly

| Metric | Non-streaming | Streaming (`_sse`, `_messages_sse`) |
|---|---|---|
| E2E latency | Yes | Yes, but ends inside the generator |
| TTFT | **Does not exist.** One response. | Yes: first `TextDelta` |
| Inter-token latency | **Does not exist** | Yes: gaps between `TextDelta`s |
| Output tokens/sec | Derivable | Derivable, more meaningful |
| Gateway overhead | Yes, and substantial | Yes, but smaller (streaming bypasses the cache) |

Two structural consequences drive the whole design:

- **Streaming returns before it runs.** `StreamingResponse` (app.py:298) hands
  back a response object *before* the provider is called. An ASGI middleware
  measuring "time until response returned" would record a number that excludes
  the entire upstream call. Streaming must self-report from inside the
  generator.
- **Streaming bypasses the cache entirely.** app.py:297 returns before any
  cache lookup, so `cached` and `streaming` can never co-occur. This collapses
  what would be two Prometheus labels into one.

## Goals

- Record E2E latency, upstream provider latency, TTFT, and inter-token latency
  for every request, on both the OpenAI-compatible and Anthropic-native
  endpoints, streaming and non-streaming.
- Make per-request latency **queryable and joinable** in Postgres, so latency
  can be compared against `model`, `key_id`, `prompt_name`,
  `prompt_version_num`, `routed_from`, and `cached` using the aggregate
  machinery `gatekeep/api/dashboard.py` already has.
- Make gateway overhead (`duration_ms - provider_ms`) computable per request,
  not just in aggregate.
- Expose correct latency distributions in Prometheus with buckets appropriate
  to LLM traffic.

## Non-goals

- **No dashboard endpoint or UI panel.** The columns are added and populated;
  surfacing them is a follow-up. This spec deliberately stops at the data
  layer so the dashboard work can be designed against real recorded data.
- **No per-segment decomposition of gateway overhead** (embedding vs. pgvector
  vs. Redis vs. DB commit). This spec makes total overhead visible; whether
  decomposing it is worth doing is a question the data from this spec should
  answer, not one to guess at now.
- **No latency input to `route_by_cost`.** Routing stays a cost-and-quality
  decision.
- **No change to existing metrics.** `request_tokens` and `request_cost_usd`
  keep their current `[model, key_id]` labels (see Cardinality below).

## Design

### Instrumentation points

Four, one concern each.

**1. ASGI middleware** - new `gatekeep/observability/latency.py`, registered on
`app` in `gatekeep/app.py`.

Stamps `request.state.started_at = time.perf_counter()` before anything else
runs, so the auth, rate-limit, and budget dependencies fall inside the window.
On the way out it observes the Prometheus E2E histogram, but **skips responses
with `media_type == "text/event-stream"`** for the reason given above.

This is the **first middleware in the application** - no `add_middleware` call
exists today - so it runs on every request, including `/healthz`, `/metrics`,
`/dashboard`, and the mounted static assets. Those never set the labels below,
so the skip rule already excludes them; no path allowlist is needed.

A middleware cannot know the labels it needs: `model` is only resolved after
translation and possible cost-based routing, and `path` is only known once the
cache lookups have run. The endpoint therefore publishes them back as it learns
them, on the same `request.state` used for the start stamp:

- `request.state.model` is set immediately after routing settles (app.py:293).
- `request.state.path` is set at each of the three non-streaming outcomes:
  `cache_exact` on an exact hit, `cache_semantic` on a semantic hit, and
  `provider` **before** the provider call is made (so a provider error still
  carries labels).

There is deliberately no `request.state.path = "stream"`. The middleware skips
SSE responses entirely, and the generator records its own observation with the
`stream` label directly, so setting it on the request would be dead state.

If either is absent when the middleware runs, the request failed before the
endpoint body got far enough to resolve a model at all: a validation error, an
auth rejection, a rate-limit or budget rejection, an unknown `prompt_name`, or
a `TranslationError` from `openai_to_payload`/`messages_to_payload` (raised for
an unsupported message role, or a request with no user/assistant message left
after system content is lifted out). Note that an unrecognized *model* is not
in this set: `resolve_route` has no unknown-model failure mode and falls
through to Ollama. The middleware skips the observation in these cases rather
than emitting an `unknown` label, so rejected traffic does not pollute the
per-model distributions. Those rejections are already counted elsewhere (the
rate-limit and budget metrics) and their latency is not what this spec is
about.

**2. Non-streaming provider calls** - the two `await provider.complete(payload)`
sites in `gatekeep/app.py` (app.py:390 in `chat_completions`, app.py:596 in
`messages`).

Inline `perf_counter()` around each. The two `provider.stream(payload)` sites
(app.py:680, app.py:750) are **not** covered here; they live inside the
generators and belong to instrumentation point 3.

Rejected alternative: a `TimedProvider` decorator wrapping `get_provider`. The
four providers are duck-typed with no shared base class, and such a wrapper
still could not capture TTFT, which has to be observed inside the delta loop.
Inline timing at two sites is less machinery for the same result.

**Endpoint signature change.** `chat_completions` (app.py:220) and `messages`
(app.py:449) currently take no `Request` parameter, so `request.state` is
unreachable from either. Both gain `request: Request` (the symbol is already
imported at app.py:20 for the exception handlers). Without this the entire
`request.state` mechanism above cannot work.

**3. The SSE generators** - `_sse` (app.py:727) and `_messages_sse`
(app.py:655).

Both gain a `started_at: float` parameter passed from their endpoint. The
first `TextDelta` sets TTFT; each subsequent delta observes an inter-token
gap; `StreamEnd` closes out E2E and already contains a `log_request` call to
carry the new columns.

**4. `log_request`** (`gatekeep/accounting.py`) gains three optional keyword
arguments, defaulting to `None`.

### Schema

Three nullable `Float` columns on `RequestLog`, one Alembic migration. Nullable
because each is genuinely undefined in some cases, not merely unknown:

| Column | Non-streamed | Streamed | Cache hit |
|---|---|---|---|
| `duration_ms` | set | set | set |
| `provider_ms` | set | set | **NULL** (no provider call was made) |
| `ttft_ms` | **NULL** (no such concept) | set | NULL |

Existing rows are `NULL` on all three. Any aggregate query must therefore
filter or coalesce explicitly; a `NULL` never means "zero latency".

**A NULL `provider_ms` is ambiguous on its own.** It means either "cache hit,
no provider call" or "row predates this migration". The two are separable only
via `cached` and `created_at`. Any consumer (notably the Phase 2 dashboard
queries) must disambiguate on `cached`, never on `provider_ms IS NULL` alone.

**Mean inter-token latency is derived, not stored:**

```
(duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0)
```

The `NULLIF` is required, not defensive styling. A single-token completion
gives a zero denominator, and `OllamaProvider.stream` yields
`eval_count or 0` (ollama.py:58), so `completion_tokens` can legitimately be
**0**, producing a denominator of `-1` and a nonsensical negative ITL. Mean ITL
is **undefined for `completion_tokens < 2`** and must be reported as such
rather than as a number.

Storing every individual inter-token gap is large write amplification for
little gain, and the tail behavior that justifies per-gap data is better served
by the Prometheus histogram.

#### What `provider_ms` measures, and its limits

Wall-clock around the provider call. For non-streaming, from just before the
SDK is invoked to when it returns: SDK request construction, TLS/HTTP, network
round trip, provider-side queueing and scheduling, token generation, response
parse. For streaming, the first iteration of `provider.stream(payload)` through
`StreamEnd`.

Three limits, stated here so they are not rediscovered as bugs:

1. **It does not separate network from provider.** A large `provider_ms` may be
   the provider being slow or gatekeep's egress being slow. Distinguishing them
   requires provider-side timing headers, which not every provider returns.
2. **It lumps pre-call and post-call gateway work together.** If overhead is
   large, `duration_ms - provider_ms` says *that* but not *where*. Locating it
   is the explicitly out-of-scope Phase 3.
3. **On the streaming path it includes client backpressure.**
   `async for ev in provider.stream(payload)` is pull-based: the loop only
   resumes when the consumer takes the next value, so a slow or stalled client
   inflates `provider_ms` and every inter-token gap with time that is not the
   provider's. TTFT is unaffected, since the first delta arrives before any
   downstream consumption. Streamed `provider_ms` is therefore *upstream time
   plus downstream drain time*, and is not comparable like-for-like with the
   non-streaming figure, which has no such contamination.

#### Known approximation in `duration_ms`

On the non-streaming path, `log_request` is called **before** the response is
returned (app.py:427). Full E2E is therefore not knowable at write time.

`duration_ms` is defined as **request start until just before `log_request`**,
which excludes JSON serialization and the socket write. True full-ASGI E2E
lives in the Prometheus histogram, recorded by the middleware. The two are
therefore *not the same number*. The gap is sub-millisecond for a JSON
response, but the discrepancy is deliberate and documented rather than
silently implied away.

On the streaming path there is no discrepancy: `log_request` fires at
`StreamEnd`, so `duration_ms` genuinely is time-to-last-token.

Rejected alternative: writing the row, then `UPDATE`-ing it from the middleware
with the true E2E. This doubles the write per request to correct a
sub-millisecond difference.

### Metrics

Five histograms in `gatekeep/observability/metrics.py`.

Default Prometheus buckets top out at 10s and are unusable for LLM traffic, so
two explicit bucket sets:

- **Wide** (E2E, provider, overhead): `0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
  0.5, 1, 2, 5, 10, 20, 30, 60, 120`. The low end matters because cache hits
  return in single-digit milliseconds.
- **Tight** (TTFT, inter-token): `0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1,
  2`.

| Metric | Labels | Recorded by |
|---|---|---|
| `gatekeep_request_duration_seconds` | `[model, path]` | middleware (all paths) |
| `gatekeep_provider_duration_seconds` | `[model]` | the two `complete` call sites, and the SSE generators |
| `gatekeep_gateway_overhead_seconds` | `[model, path]` | endpoint and generators, at log time |
| `gatekeep_ttft_seconds` | `[model]` | SSE generators |
| `gatekeep_inter_token_seconds` | `[model]` | SSE generators |
| `gatekeep_time_to_last_token_seconds` | `[model]` | SSE generators |

`path` takes one of `cache_exact`, `cache_semantic`, `provider`, `stream`. A
single label replaces separate `cached` and `streaming` labels because, as
established above, those can never co-occur.

Overhead gets a real histogram rather than being derived in PromQL, because
subtracting two histograms is not a statistically valid operation.

#### Three caveats on reading these metrics

**Revised 2026-08-03: `request_duration_seconds` had two definitions of E2E,
and no longer does.** As originally shipped, `path="stream"` meant start until
the last token (recorded by the generator) while every other path meant the
full ASGI span (recorded by the middleware), so any query that did not pin
`path` aggregated two incomparable spans. The premise was wrong: the ASGI call
does not return until the streamed body is fully sent, so the middleware can
time streaming after all. It now records every path, and start-until-last-token
moved to `gatekeep_time_to_last_token_seconds{model}`, which sits alongside
`ttft_seconds` and `inter_token_seconds` as the third streaming-only,
`(model,)`-labeled metric.

**`gateway_overhead_seconds` is not `request_duration` minus
`provider_duration`.** It is computed at log time from the same duration the
`duration_ms` column uses, which stops just before `log_request` (see the
approximation note above), whereas `request_duration_seconds` is the full ASGI
span. The difference is sub-millisecond, but the three metrics are not exactly
algebraically consistent and should not be assumed to be. On the streaming
path the gap is larger than sub-millisecond, since overhead is computed
against time-to-last-token while `request_duration_seconds` runs to the end of
the response body.

**On a cache hit, overhead equals the entire duration.** There is no provider
call, so `provider_ms` is NULL and all elapsed time is gatekeep's own. The
observation is still recorded, with `path="cache_exact"` or
`path="cache_semantic"` and the full duration as the value. This is deliberate:
"how fast is a cache hit" is one of the more useful things this feature can
answer, and skipping the observation would throw it away.

#### Cardinality

Latency histograms are labeled `[model, path]` and deliberately **not**
`key_id`. The wide bucket set yields 18 series per label combination (15
buckets, `+Inf`, `_sum`, `_count`). At 6 models and 200 keys:

- Without `key_id`: 5 x 6 x ~2 paths x 18 ~= **1,100 series**
- With `key_id`: 5 x 6 x 200 x 18 ~= **108,000 series**

What this costs: per-key latency is unavailable as a real-time Prometheus
alerting or Grafana source. What it does not cost: per-key latency analysis
generally. `_base_filters` (dashboard.py:84) already filters on
`RequestLog.key_id`, so per-key latency is queryable from Postgres through
existing machinery once the columns exist, and `percentile_cont` over raw rows
is *more* accurate than a Prometheus histogram quantile, which interpolates
within buckets.

In fairness: the existing `request_tokens` and `request_cost_usd` histograms
are already labeled `[model, key_id]`, roughly 40,000 series at the same
numbers. Dropping `key_id` here does not fix that exposure, it avoids roughly
tripling it. Revisiting the existing labels is worthwhile but is a separate
change and is out of scope.

#### Inter-token latency is really inter-chunk latency

Providers do not guarantee one token per delta. Anthropic's `text_stream`
yields text pieces; Ollama yields per-token. So `gatekeep_inter_token_seconds`
measures gaps between *chunks*, whatever size the provider chose.

The DB-derived mean ITL, which divides by `completion_tokens`, **is** properly
token-normalized. The two are complementary: Prometheus carries true tail
behavior in provider-native units, Postgres carries a token-normalized mean.
Neither is the single truth and neither should be presented as such.

### Error paths

- **Provider error, non-streaming**: `map_provider_error` returns early and no
  `RequestLog` row is written today. That does not change. The middleware still
  observes E2E, so failed-request latency remains visible in Prometheus.
- **Mid-stream error**: the generator's `except` (app.py:780) emits an in-band
  error event. No `StreamEnd` arrives, so no row is written and TTFT may or may
  not have been set. Unchanged.
- **A timing failure must never fail a request.** Instrumentation is
  best-effort in the same spirit as the `record_spend` Redis call: a missing
  `request.state.started_at` (for example on a request path that bypassed the
  middleware) results in `None` columns and no observation, never an exception.

### Testing

Existing tests inject fake providers, so a fake that sleeps deterministically
between deltas yields assertable TTFT and ITL.

- Non-streaming populates `duration_ms` and `provider_ms`, leaves `ttft_ms`
  NULL.
- Streaming populates all three, with `ttft_ms <= duration_ms`. The comparison
  is deliberately non-strict: a fake provider that yields without awaiting can
  produce equal values at float resolution, and a strict `<` would be a flaky
  test.
- Exact and semantic cache hits populate `duration_ms`, leave `provider_ms`
  and `ttft_ms` NULL, and still observe `gateway_overhead_seconds`.
- `duration_ms >= provider_ms` on every row where both are set.
- A completion with `completion_tokens` of 0 or 1 does not produce a divide-by-
  zero or a negative derived ITL.
- The middleware does not observe E2E for `text/event-stream` responses.
- Each histogram observes with the expected label set, asserted via the
  Prometheus registry.
- Both endpoints (`/v1/chat/completions` and `/v1/messages`) are covered on
  both streaming and non-streaming paths.
- A request missing `request.state.started_at` still succeeds.

## Files

| File | Change |
|---|---|
| `gatekeep/observability/latency.py` | New. ASGI middleware. |
| `gatekeep/observability/metrics.py` | Five histograms, two bucket sets. |
| `gatekeep/models.py` | Three columns on `RequestLog`. |
| `migrations/versions/0011_request_latency.py` | New Alembic revision. |
| `gatekeep/accounting.py` | Three optional kwargs on `log_request`. |
| `gatekeep/app.py` | Register middleware; add `request: Request` to `chat_completions` and `messages`; set `request.state.model`/`path`; time the two `complete` call sites; thread `started_at` into both SSE generators; observe TTFT/ITL. |
| `tests/*` | Coverage per above. |
