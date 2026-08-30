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
- Rate limiting is a Redis token bucket **pooled per account** (not per key),
  with capacity/refill (`rate_limit_tokens_per_min` default 100,
  `rate_limit_refill_rate`) read once from process-wide `Settings` -
  `require_rate_limit` (`gatekeep/middleware/ratelimit.py`) applies the same
  capacity to every account in the process; there is no per-account or per-key
  override anywhere in the schema. A coarser per-IP pre-auth limit
  (`pre_auth_rate_limit_tokens_per_min`, default 300) works the same way. Both
  are env-configurable via `Settings`, but only as one process-wide value.
- Budgets enforce at `spend >= monthly_budget_usd`, which lives on `Account`
  (not `ApiKey`) and so *can* differ per account - set via
  `gatekeep account create --budget` / `gatekeep key set-budget`. Backed by a
  Redis spend counter reconciled against the `request_logs` DB aggregate by a
  background loop.
- `is_billed_provider(provider)` (`gatekeep/routing/pricing.py`) returns true
  only for anthropic/openai/google, via a hardcoded `BILLED_PROVIDERS`
  frozenset. Ollama is treated as free. Pricing lookup
  (`PricingTable.lookup`) is exact-match on `(provider, model)` against the
  vendored/override JSON table. `is_unpriced(provider, model)` is
  `is_billed_provider(provider) and lookup(...) is None`, and
  `enforce_pricing_policy` calls `is_unpriced` **before** any upstream call and
  applies `pricing_miss_policy` (default `reject`) to it - a billed provider
  with no table entry is rejected by default, not billed at $0.
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
  `settings.loadtest_stub_enabled` is true**. `_providers` (`gatekeep/app.py`)
  is built once at import time from the module-level `_settings = get_settings()`,
  so this is a plain `if _settings.loadtest_stub_enabled:` guard at that same
  scope, not deferred logic. When the flag is off and a `stub/` request
  arrives, `get_provider("stub")` misses and the request returns the normal
  unknown-provider error path - so the stub is inert in production regardless
  of the routing change. The `_GatewayProvider` type alias (currently
  `AnthropicProvider | OllamaProvider | OpenAIProvider | GoogleProvider`) widens
  to include `StubProvider`.
- New config field in `gatekeep/config.py`:
  `loadtest_stub_enabled: bool = False`, documented as load-testing-only and
  never to be enabled in production.

### 3. Stub billing

To exercise cost accounting and budget enforcement for real, a stub request must
produce cost > 0 - but it must never be caught by `pricing_miss_policy` along
the way, since that policy exists to protect against a real billed model that
gatekeep has failed to price, and defaults to rejecting the request outright.
`stub` is never a table entry (there are unboundedly many `stub/*` variants,
and the exact-match table cannot wildcard them), so the naive reading -
"add `stub` to `BILLED_PROVIDERS`" - would make `is_unpriced("stub", model)`
true for every stub request and reject all of them under the default `reject`
policy. Instead, `stub` is billed **without ever being treated as unpriced**:

- `is_billed_provider` and `is_unpriced` (`gatekeep/routing/pricing.py`)
  **do not change** - `stub` is deliberately kept out of `BILLED_PROVIDERS`, so
  `enforce_pricing_policy` sees `is_unpriced("stub", model) == False` and always
  returns `None` (proceed), regardless of `pricing_miss_policy`. The unpriced-
  model metric/alert path is for real billed providers only and must never fire
  for stub traffic.
- `calculate_cost` (`gatekeep/accounts/accounting.py`) gains a branch checked
  **before** the `BILLED_PROVIDERS`/table-miss logic: `if provider == "stub":
  return <fixed-price>.cost(prompt_tokens, completion_tokens)` when
  `loadtest_stub_enabled` is true, applying a **single fixed per-1M-token
  price to all `stub/*` models** (both input and output). This is the one
  dedicated branch mentioned above - a cost calculation keyed on the `stub`
  provider name, not a pricing-table entry.
- The fixed price is a constant (documented; a nominal value such that a modest
  budget is exhausted within a scenario's run). Because it is deterministic,
  budget-enforcement scenarios can compute the exact request count at which the
  block should fire.
- When `loadtest_stub_enabled` is false, the branch is unreachable (`stub` is
  not registered as a provider at all, per §2), so cost/pricing behavior for
  every real provider is untouched.

Consequence: a stub request is never rejected or alerted on as unpriced, always
produces cost > 0, and the budget-enforcement scenario asserts the block fires
at the predicted spend, with the Redis counter reconciling against
`request_logs` with no drift under concurrency.

### 4. Load-test environment - `loadtest/docker-compose.loadtest.yml`

A compose override layered on the base `docker-compose.yml`:

- Gateway env: `LOADTEST_STUB_ENABLED=true`; raised `RATE_LIMIT_TOKENS_PER_MIN`
  and `PRE_AUTH_RATE_LIMIT_TOKENS_PER_MIN` (with matching refill rates) so the
  limiter - which is one process-wide value shared by every account (see
  "Relevant existing architecture") - is not the bottleneck in
  throughput/latency/breaking-point scenarios; `PRICING_MISS_POLICY` left at
  default (`stub` is never treated as unpriced, per §3, so the policy never
  applies to it either way).
- Runs the gateway single-worker first for clean per-request overhead numbers.
  The override documents (commented) how to scale to N uvicorn workers /
  replicas for a second capacity pass.
- Postgres/Redis/Prometheus/Grafana come from the base compose unchanged.

### 5. Key bootstrap - `loadtest/bootstrap.py`

Mints API keys and writes them to a git-ignored `loadtest/keys.json`:

- A pool of many keys, on high-budget (or unlimited) accounts, for
  throughput/latency/breaking-point scenarios. Rate limiting is one
  process-wide value applied to every account (see "Relevant existing
  architecture"), already raised well above target load by the compose
  override in §4, so there is no per-key limit to size a pool against here.
- A small number of keys on **dedicated low-budget accounts** (`gatekeep
  account create --budget <n>`) for the budget half of the enforcement
  scenario. There is no differentiated low-rate-limit key pool - rate-limit
  capacity cannot vary per account or per key in the current architecture, so
  429 behavior is exercised once, globally, in the breaking-point scenario
  (§6) rather than as a targeted low-limit-key case. Adding a real per-account
  rate-limit override is out of scope for v1 (see "Out of scope").

Reuses the existing key-minting service/CLI code path rather than inserting rows
directly, so keys are created exactly as in production.

### 6. Locust harness - `loadtest/locustfile.py`

User classes / tasks mapped to the four goals, each parameterized to span the
distinct latency paths (non-streaming, streaming, cache-hit, cache-miss):

1. **Throughput / capacity** - a `LoadTestShape` ramping RPS across the key
   pool; find max RPS before p95 overhead climbs or errors appear.
2. **Latency SLO** - fixed moderate RPS; record gateway-overhead percentiles
   (server-side) and client latency, assert against targets.
3. **Breaking point** - ramp until failure; observe error rate, DB pool
   exhaustion, Redis errors, timeouts, and - since rate-limit capacity is one
   process-wide value shared by every account (not a per-key dial) - confirm
   429s appear at the expected aggregate RPS with no over-admission once the
   account(s) in flight exceed it.
4. **Enforcement under concurrency** - saturate one low-budget key (expect the
   budget block at the predicted spend, no over-spend past it); afterwards
   verify the Redis spend counter reconciles against `request_logs` with no
   drift under concurrency. (Rate-limit exactness is covered by the
   breaking-point scenario above, not a dedicated low-limit key - see §5.)

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
- **Cardinality guardrail:** `gateway_overhead_seconds`, `request_duration_seconds`,
  and `provider_duration_seconds` are labeled by `model`, and §1's stub design
  deliberately encodes latency/size/ITL into the model string so distinct
  parameterizations get distinct cache keys. Left unchecked, a scenario that
  sweeps many parameterizations (e.g. a breaking-point ramp trying several
  latency/size combinations) would mint a new `model` label value per variant
  on process-lifetime histograms - a real memory/cardinality cost on the
  Prometheus instance. Each scenario therefore drives a small, fixed,
  documented set of stub model strings (one or two per latency-path being
  exercised) rather than generating parameterizations dynamically; the runbook
  enumerates the exact set per scenario.
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
- Billing test: with the flag on, a stub request is never treated as unpriced
  (no `unpriced_model_total` increment, no rejection under the default
  `pricing_miss_policy`), records cost > 0, and decrements the budget; the
  enforcement block fires at the predicted spend.
- Keep the full pytest suite and ruff/pre-commit green.

## Out of scope (YAGNI)

- A per-account or per-key rate-limit override. Rate limiting stays one
  process-wide value shared by every account, as it is today; the
  breaking-point scenario exercises 429 behavior at that shared capacity
  instead of via a dedicated low-limit key (see §5/§6).
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
