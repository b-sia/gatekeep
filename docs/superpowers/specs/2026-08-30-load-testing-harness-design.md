# Load-Testing Harness Design

**Date:** 2026-08-30
**Status:** Approved for planning
**Branch:** `test/load-testing-harness`

## Problem

Gatekeep has been built to handle production workload but has never been load
tested. We need a repeatable way to measure the gateway's own capacity and
behavior under concurrent load, targeting four goals:

1. **Max throughput / capacity** - peak sustainable RPS and the resource
   ceiling (CPU, DB connections, Redis) before latency degrades.
2. **Latency SLOs under load** - p50/p95/p99 gateway overhead stays within
   target as concurrency rises.
3. **Breaking point & failure modes** - how the gateway degrades past capacity
   (connection-pool exhaustion, timeouts, error rates).
4. **Enforcement under concurrency** - rate limits, budgets, and caching behave
   correctly under heavy concurrent load, with no leaks or races.

## Core constraint

For an LLM gateway, the upstream provider call dominates latency and, for billed
providers, costs real money per request. To measure the *gateway's own* capacity
(auth, rate limiting, budget checks, cache lookups, cost accounting, the
per-request Postgres write, routing) we must isolate it from real provider
latency and cost. The design does this with an in-app **stub provider** that
returns canned responses with tunable, deterministic latency and output size, at
zero cost.

## Chosen approach

Approach A (of three considered): an in-app, flag-gated stub provider plus a
Locust harness driven by a docker-compose override, run against the local stack.

Rejected alternatives:

- **B - external mock upstream container.** Exercises the real provider SDK +
  httpx connection-pool path, but adds infra, must faithfully mimic each SDK's
  wire format and streaming, and makes token/latency control harder. Its one
  advantage (real httpx pool) can be added to A later as a second mode. Deferred.
- **C - warm exact-cache + Ollama only, no code.** Zero changes, but cannot
  tune latency/size, makes Ollama (not the gateway) the bottleneck, and only
  ever exercises the cache-hit path or a slow local model. Cannot answer the
  capacity/breaking-point questions.

## Relevant existing architecture

- Providers are a `dict[str, _GatewayProvider]` (`_providers` in
  `gatekeep/app.py`), looked up by `get_provider(name)`. Each provider
  implements `complete(payload) -> CompletionResult` and
  `stream(payload) -> AsyncIterator[TextDelta | StreamEnd]`
  (`gatekeep/providers/base.py`).
- `resolve_route(requested, *, aliases)` in `gatekeep/api/translation.py`
  dispatches by model prefix: `openai/` and `google/` route directly to those
  upstreams with the prefix stripped; `claude-` and alias-table entries route to
  Anthropic; everything else falls through to Ollama. Return type is
  `tuple[Literal["anthropic","ollama","openai","google"], str]`.
- Rate limiting is a Redis token bucket, per API key
  (`rate_limit_tokens_per_min`, default 100) plus a coarser per-IP pre-auth
  limit (`pre_auth_rate_limit_tokens_per_min`, default 300). Both are already
  env-configurable via `Settings`.
- Budgets enforce at `spend >= monthly_budget_usd`, backed by a Redis spend
  counter reconciled against the `request_logs` DB aggregate by a background
  loop.
- `is_billed_provider(provider)` (`gatekeep/routing/pricing.py`) returns true
  only for anthropic/openai/google. Ollama is treated as free. Pricing lookup is
  exact-match on `(provider, model)`; `pricing_miss_policy` (default `reject`)
  governs an unpriced billed model.
- Every request writes one row via `log_request` (Postgres) and touches Redis
  for rate limit, budget counter, and cache. Prometheus metrics are already
  defined in `gatekeep/observability/metrics.py` (notably
  `gateway_overhead_seconds`, `request_duration_seconds{path}`,
  `provider_duration_seconds`, cache hit/miss counters,
  `rate_limit_rejections_total`, budget/unpriced alert counters) and scraped
  into Grafana.

## Component design

### 1. Stub provider - `gatekeep/providers/stub.py`

A `StubProvider` class matching the existing provider protocol:

- `complete(payload)` sleeps for the configured latency, then returns a
  `CompletionResult` with canned text sized to the requested output-token count,
  and plausible `input_tokens` (estimated from the payload) / `output_tokens`.
- `stream(payload)` sleeps once for the initial latency (drives TTFT), then
  yields `TextDelta` chunks separated by a configured inter-token delay, followed
  by a terminal `StreamEnd` carrying final usage.

**Behavior is taken from the model string**, so any OpenAI/Anthropic client can
drive it by setting `model` alone, and so distinct parameterizations produce
distinct cache keys:

- `stub/lat50-out200` -> 50 ms latency, 200 output tokens.
- An optional inter-token component for streaming, e.g. `stub/lat50-out200-itl5`
  -> 5 ms between deltas. When omitted, a default derived from latency/size.
- `stub/default` (or an unparseable suffix) -> documented defaults.

Parsing is total and forgiving: unknown or malformed segments fall back to
defaults rather than erroring, so a load script never fails on a typo mid-run.
The canned text is deterministic for a given size (so exact-cache scenarios get
stable hits).

### 2. Routing and gating

- `resolve_route` gains a `stub/` prefix branch returning
  `("stub", <stripped>)`, and its `Literal` return type adds `"stub"`. This is a
  pure function change; it maps the prefix unconditionally.
- The `stub` entry is added to `_providers` **only when
  `settings.loadtest_stub_enabled` is true**. When the flag is off and a `stub/`
  request arrives, `get_provider("stub")` misses and the request returns the
  normal unknown-provider error path - so the stub is inert in production
  regardless of the routing change.
- New config field in `gatekeep/config.py`:
  `loadtest_stub_enabled: bool = False`, documented as load-testing-only and
  never to be enabled in production.

### 3. Stub billing

To exercise cost accounting and budget enforcement for real, a stub request must
produce cost > 0. Mechanism:

- When `loadtest_stub_enabled` is true, treat `stub` as a **billed** provider at
  a **single fixed per-1M-token price applied to all `stub/*` models** (both
  input and output). This is a small dedicated branch in the pricing/accounting
  path keyed on the `stub` provider name, rather than a pricing-table entry per
  latency/size variant (there are unboundedly many variants, and the exact-match
  table cannot wildcard them).
- The fixed price is a constant (documented; a nominal value such that a modest
  budget is exhausted within a scenario's run). Because it is deterministic,
  budget-enforcement scenarios can compute the exact request count at which the
  block should fire.
- When the flag is off, `stub` is neither billed nor priced (moot, since it is
  not registered).

Consequence: the budget-enforcement scenario asserts the block fires at the
predicted spend, and that the Redis counter reconciles against `request_logs`
with no drift under concurrency.

### 4. Load-test environment - `loadtest/docker-compose.loadtest.yml`

A compose override layered on the base `docker-compose.yml`:

- Gateway env: `LOADTEST_STUB_ENABLED=true`; raised `RATE_LIMIT_TOKENS_PER_MIN`
  and `PRE_AUTH_RATE_LIMIT_TOKENS_PER_MIN` (with matching refill rates) so the
  limiter is not the bottleneck in throughput/latency/breaking-point scenarios;
  `PRICING_MISS_POLICY` left at default (stub is explicitly billed, so no miss).
- Runs the gateway single-worker first for clean per-request overhead numbers.
  The override documents (commented) how to scale to N uvicorn workers /
  replicas for a second capacity pass.
- Postgres/Redis/Prometheus/Grafana come from the base compose unchanged.

### 5. Key bootstrap - `loadtest/bootstrap.py`

Mints API keys and writes them to a git-ignored `loadtest/keys.json`:

- A pool of many high-budget, high-rate-limit keys for throughput/latency/
  breaking-point scenarios (so per-key limits never gate the aggregate).
- A small number of deliberately low-rate-limit and low-budget keys for the
  enforcement scenario.

Reuses the existing key-minting service/CLI code path rather than inserting rows
directly, so keys are created exactly as in production.

### 6. Locust harness - `loadtest/locustfile.py`

User classes / tasks mapped to the four goals, each parameterized to span the
distinct latency paths (non-streaming, streaming, cache-hit, cache-miss):

1. **Throughput / capacity** - a `LoadTestShape` ramping RPS across the
   high-limit key pool; find max RPS before p95 overhead climbs or errors
   appear.
2. **Latency SLO** - fixed moderate RPS; record gateway-overhead percentiles
   (server-side) and client latency, assert against targets.
3. **Breaking point** - ramp until failure; observe error rate, DB pool
   exhaustion, Redis errors, timeouts.
4. **Enforcement under concurrency** - saturate one low-limit key (expect exact
   429 counts, no over-admission), then one low-budget key (expect the budget
   block at the predicted spend); afterwards verify the Redis spend counter
   reconciles against `request_logs`.

Cache behavior is controlled per task: cache-hit tasks send an identical request
(stable stub text -> stable hash), cache-miss tasks vary the prompt. Streaming
vs non-streaming is a per-task flag.

Configuration via environment: `TARGET_HOST`, keys-file path, and scenario
parameters. Each run exports Locust CSV under `loadtest/results/`.

### 7. Metrics, thresholds & reporting

- **Client-side (Locust):** RPS, latency percentiles, failure rate, per scenario
  (CSV).
- **Server-side (existing Prometheus/Grafana):** `gateway_overhead_seconds`,
  `request_duration_seconds{path}`, `provider_duration_seconds`, cache hit
  rates, `rate_limit_rejections_total`, budget/unpriced alert counters, plus
  DB/Redis infra metrics. No new dashboards required for v1; the runbook lists
  the exact panels/queries to read for each scenario.
- **Deliverables:** `loadtest/README.md` runbook (how to run each scenario, what
  to read, how to interpret) and a results template to record baselines over
  time.
- **Draft SLOs (placeholders, calibrated from the first baseline run):**
  cache-hit gateway overhead p95 < 15 ms; stub non-streaming overhead p95
  < 25 ms; error rate < 0.1% below capacity. Real targets are set from the first
  baseline and recorded in the results template.

### 8. Justfile targets

- `loadtest-up` - bring up the stack with the override.
- `loadtest-bootstrap` - mint keys into `loadtest/keys.json`.
- `loadtest <scenario>` - run one scenario against the running stack.
- `loadtest-down` - tear down the load-test stack.

## Testing

- Unit tests for `StubProvider`: model-string parsing (including malformed
  fallback), latency behavior, output-token sizing, non-streaming result shape,
  and streaming (TTFT delay, inter-token deltas, terminal `StreamEnd` usage).
- Routing test: `resolve_route` maps `stub/...` to `("stub", ...)`.
- Smoke test: with the flag on, a `stub/` request returns 200 with a
  well-formed body; with the flag off, `stub/` does not resolve to a stub
  provider (inert).
- Billing test: with the flag on, a stub request records cost > 0 and decrements
  the budget; the enforcement block fires at the predicted spend.
- Keep the full pytest suite and ruff/pre-commit green.

## Out of scope (YAGNI)

- Distributed multi-machine Locust workers (single-machine workers suffice
  locally).
- CI-gated performance thresholds (future work once baselines exist).
- Approach B's external mock-upstream mode / real-httpx-pool path (future second
  mode).
- Load testing against a dedicated/staging host (this design targets the local
  docker-compose stack; the harness is host-agnostic via `TARGET_HOST`, so a
  staging run is a later config change, not new code).

## Open questions

None blocking. Draft SLO numbers and the fixed stub price are deliberately
provisional and will be finalized from the first baseline run.
