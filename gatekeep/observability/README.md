# Observability

`GET /metrics` is a Prometheus-format endpoint (unauthenticated, like `/healthz`)
and is Gatekeep's **integration surface**: scrape it into whatever Prometheus you
already run.

`docker-compose.yml` also brings up Prometheus and a small "Gatekeep - Ops" Grafana
dashboard at `http://localhost:3000`, as a local-development convenience. That
dashboard is deliberately scoped to signals Postgres structurally cannot serve:
rate-limit rejections, budget alerts, and real-time latency tails. Cost, savings,
cache-hit rate, and latency attribution live on [`/dashboard`](../../dashboard/README.md),
which computes them exactly from `request_logs` rather than extrapolating from
histogram buckets.

| File | Contents |
|---|---|
| `metrics.py` | Every `gatekeep_*` metric definition and its observation helpers |
| `latency.py` | Latency span bookkeeping shared by the request paths |
| `prometheus.yml`, `grafana.json`, `provisioning/` | What `docker-compose` provisions |

## Traffic and cost metrics

| Metric | Labels | Meaning |
|---|---|---|
| `gatekeep_requests_total` | `model` | Chat completion requests |
| `gatekeep_request_tokens` | `model` | Prompt + completion tokens per request |
| `gatekeep_request_cost_usd` | `model` | USD cost per request |
| `gatekeep_rate_limit_rejections_total` | - | Requests rejected with 429 for rate limit |
| `gatekeep_budget_alerts_total` | `threshold` | Budget alerts, `warning` or `exceeded` |
| `gatekeep_cache_exact_hits` / `_misses` | `model` | Exact-cache outcomes |
| `gatekeep_cache_semantic_hits` / `_misses` | `model` | Semantic-cache outcomes |
| `gatekeep_cache_semantic_similarity` | `model` | Observed similarity on a semantic hit |
| `gatekeep_cache_cost_saved_usd` | - | Cumulative USD saved by serving cache hits |

## Latency metrics

- `gatekeep_request_duration_seconds{model,path}` - end-to-end latency, the full
  request span on every `path`, recorded in one place. `path` is one of
  `cache_exact`, `cache_semantic`, `provider`, `stream`. Aggregating across paths is
  valid, but the distributions differ by orders of magnitude, so a pinned `path` is
  usually the more useful query.
- `gatekeep_time_to_last_token_seconds{model}` - request start until the last
  streamed token, streaming only. Smaller than
  `gatekeep_request_duration_seconds{path="stream"}` for the same request, which
  also covers the trailing SSE events and response teardown.
- `gatekeep_provider_duration_seconds{model}` - time in the upstream call. On the
  streaming path this includes downstream backpressure, since the stream loop is
  pull-based, so it is not comparable like-for-like with the non-streaming figure.
- `gatekeep_gateway_overhead_seconds{model,path}` - request time not spent
  upstream, computed by the same middleware as `request_duration_seconds` from the
  same span, so `overhead = duration - provider` holds exactly on every path. On a
  cache hit this is the entire duration.
- `gatekeep_ttft_seconds{model}` - time to first token, streaming only.
- `gatekeep_inter_token_seconds{model}` - gap between streamed deltas. This is
  really inter-*chunk* latency: providers do not guarantee one token per delta. The
  token-normalized figure is derived from `request_logs` instead, as
  `(duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0)`, which is undefined
  below two completion tokens.

## Latency on `request_logs`

Per-request latency is also stored on `request_logs` as `duration_ms`,
`provider_ms`, `ttft_ms`, and `path`.

- `provider_ms` is NULL on a cache hit. A NULL `provider_ms` alone cannot
  distinguish a cache hit from a row predating the migration - filter on `cached`.
- `ttft_ms` is NULL on any non-streamed request.
- `path` carries the same four values as the Prometheus `path` label. It is NULL
  only on rows predating migration `0012`, which every latency query excludes.
- `duration_ms` means two different things depending on `path`: end-to-end on the
  non-streaming paths, and time-to-last-token on `stream`. Percentiles are never
  blended across the two.

Each `path` value comes from a module-level constant in `gatekeep/app.py` rather
than a repeated string literal. On the non-streaming paths a `_finish_request`
parameter carries the constant into both `mark()` and `log_request()`; on the
streaming path the `mark()` call and the SSE generator's `log_request()` call each
read `_STREAM_PATH` directly, because they run in two different functions with no
shared parameter to carry it. Either way the metric label and the DB column cannot
drift apart from a typo in one of them.

## Why `key_id` is not a metric label

Per-key and per-prompt latency attribution lives on `/dashboard`, not in
Prometheus. The latency histograms use a wide bucket set, so adding `key_id` would
push the series count roughly two orders of magnitude higher.
