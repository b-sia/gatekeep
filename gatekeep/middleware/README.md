# Request middleware

Every request passes through these checks, in order, before the gateway will call a
provider. Each stage is a FastAPI dependency, so a rejection short-circuits the
request without any upstream cost.

```
auth -> rate limit -> budget -> exact cache -> semantic cache -> provider
```

| Module | Responsibility |
|---|---|
| `auth.py` | Resolves the `Authorization: Bearer gk-...` header to an `ApiKey` row |
| `ratelimit.py` | Per-key token bucket in Redis |
| `budget.py` | Per-key monthly USD spend cap and alerts |
| `cache_exact.py` | Redis exact-match response cache |
| `cache_semantic.py` | Postgres + `pgvector` embedding-similarity cache |

## Rate limiting

Each key gets a per-minute token bucket, `rate_limit_tokens_per_min` (default 100)
refilled at `rate_limit_refill_rate` tokens/second. Once the bucket is empty the
gateway returns 429 with a `Retry-After` header instead of forwarding:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3
```

Rejections increment `gatekeep_rate_limit_rejections_total`.

## Budgets

A key with `monthly_budget_usd` set is blocked once its current calendar-month
spend reaches that cap; the request fails with a 429 carrying an OpenAI-shaped
error body. Keys with `monthly_budget_usd` unset are unlimited and skip the check
entirely.

```bash
gatekeep key set-budget my-key 25.00     # cap this key at $25/month
gatekeep key set-budget my-key --unlimited
```

Spend is tracked in a Redis counter per key and period, seeded from `request_logs`
if the counter is missing, and reset implicitly at each month boundary because the
period label (`YYYY-MM`) is part of the counter key. Counters carry a 40-day TTL.

Two alert levels increment `gatekeep_budget_alerts_total{threshold=...}`:
`warning` at `BUDGET_ALERT_THRESHOLD` of the cap (default `0.8`) and `exceeded` at
100%. Alerts are purely observational - enforcement always blocks at
`spend >= monthly_budget_usd` regardless of the warning threshold.

Cache hits contribute $0 to the budget counter, even though `request_logs.cost_usd`
still records the full notional cost of the response. That is deliberate: the log
column measures what the traffic would have cost, paired with
`gatekeep_cache_cost_saved_usd` as the discount, while the budget counter measures
money actually spent upstream.

## Caching

Non-streaming requests are checked against two caches before the provider is
called:

- **Exact cache** (Redis) - identical requests (same model, messages, `max_tokens`,
  and so on) are served from cache within `cache_exact_ttl_seconds`, default 7
  days.
- **Semantic cache** (Postgres + `pgvector`) - a request whose embedding is more
  similar than `semantic_cache_similarity_threshold` (default `0.95` cosine
  similarity) to a previously cached request is served that cached answer, even
  with different wording. Observed similarities are recorded in
  `gatekeep_cache_semantic_similarity`.

Both kinds of hit skip the provider call entirely and are logged to `request_logs`
with `cached: true`, so cache-hit rate and savings show up in cost accounting the
same way normal requests do. Hits and misses are counted per tier in
`gatekeep_cache_{exact,semantic}_{hits,misses}`.

Streaming requests bypass both caches.

The semantic cache's embedding model is warmed at startup, off the event loop, so
the first request after boot does not pay the model-load cost.

### Prompt-version invalidation

Caching is prompt-aware. Promoting a new version of a registered prompt
(`gatekeep prompt promote <name> <version>`) invalidates cached responses built
from the old version, so clients never see an answer generated from a prompt that
is no longer active.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `RATE_LIMIT_TOKENS_PER_MIN` | `100` | Bucket size per key |
| `RATE_LIMIT_REFILL_RATE` | `100/60` | Bucket refill, tokens per second |
| `CACHE_EXACT_TTL_SECONDS` | `604800` | Exact-cache TTL (7 days) |
| `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` | `0.95` | Minimum cosine similarity for a semantic hit |
| `BUDGET_ALERT_THRESHOLD` | `0.8` | Fraction of the cap at which the `warning` alert fires |
