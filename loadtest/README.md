# Load-Testing Gatekeep

Measures the gateway's own request-handling overhead (auth, rate limiting,
budget checks, cache lookups, cost accounting, the per-request Postgres
write, routing) in isolation from any real upstream provider, using an
in-app stub provider. See
`docs/superpowers/specs/2026-08-30-load-testing-harness-design.md` for the
full design.

**Never set `LOADTEST_STUB_ENABLED=true` outside this local workflow.**

## Setup

```bash
just loadtest-up          # bring up postgres/redis/ollama/prometheus/grafana
                           # + gateway with the stub enabled and rate limits raised
just loadtest-bootstrap    # mint keys into loadtest/keys.json (git-ignored)
pip install -e ".[loadtest]"
```

Grafana is at `http://localhost:3000` (anonymous viewer access), Prometheus
at `http://localhost:9090`.

## Running a scenario

```bash
just loadtest ThroughputUser     # goal 1: max sustainable RPS
just loadtest LatencyUser        # goal 2: latency SLO at fixed concurrency
just loadtest BreakingPointUser  # goal 3: ramp past capacity, observe failure modes
just loadtest EnforcementUser    # goal 4: budget cap under concurrency
```

Each run writes a Locust CSV to `loadtest/results/<ScenarioName>_*.csv`
(git-ignored).

`ThroughputUser` and `BreakingPointUser` are shape-driven (they define their
own `LoadTestShape`), and Locust runs a shape's full stage list to
completion before it even consults `-t`/`--run-time` - so shortening `-t`
has no effect on those two scenarios. To stop one of them early, interrupt
it with Ctrl+C/SIGINT; Locust shuts down gracefully and still prints the
summary. `LatencyUser` and `EnforcementUser` use `-u`/`-r`/`-t` directly, so
`-t` does bound those runs normally. See `loadtest/locustfile.py`'s module
docstring for the full detail.

Every scenario sends only two stub model strings -
`stub/lat50-out200` (non-streaming and cache paths) and
`stub/lat50-out200-itl5` (streaming) - deliberately fixed rather than swept,
to avoid minting new `model` label values on the process-lifetime Prometheus
histograms below. Do not add more without checking that guardrail still
holds.

## What to read, per scenario

**Client-side (Locust's own console/CSV output):** RPS, latency percentiles,
failure rate.

**Server-side (Grafana / Prometheus queries):**

| Scenario | Panels / queries |
|---|---|
| ThroughputUser | `histogram_quantile(0.95, rate(gatekeep_gateway_overhead_seconds_bucket[1m]))`; `rate(gatekeep_request_duration_seconds_count{path=~".+"}[1m])` (RPS); error rate from Locust. The step where p95 first climbs sharply is the practical capacity ceiling. |
| LatencyUser | `histogram_quantile(0.5\|0.95\|0.99, rate(gatekeep_gateway_overhead_seconds_bucket[5m]))`, split by whether the request hit cache (`gatekeep_cache_exact_hits`/`gatekeep_cache_exact_misses` rates) - compare against the draft SLOs below. |
| BreakingPointUser | Locust failure rate and error types; `rate(gatekeep_rate_limit_rejections_total[1m])` (confirm 429s appear once aggregate RPS exceeds the raised process-wide limit, with no over-admission before it); Postgres/Redis connection and error metrics (host-level or `docker compose logs postgres redis`). |
| EnforcementUser | HTTP 429 `budget_exceeded_error` responses in Locust's failure log, appearing once the low-budget key's account crosses `monthly_budget_usd`; confirm no further 200s after the block starts. |

## Draft SLOs (placeholders - replace with the first baseline's numbers)

- Cache-hit gateway overhead p95 < 15 ms
- Stub non-streaming overhead p95 < 25 ms
- Error rate < 0.1% below capacity

## Results log

Record one row per baseline run. Keep this table in this file (not a
separate results file) so history and methodology stay together.

| Date | Scenario | Max sustainable RPS | p50 / p95 / p99 overhead (ms) | Error rate | Notes |
|---|---|---|---|---|---|
| | | | | | |

## Scaling to multiple gateway workers

The default `loadtest-up` runs the gateway single-worker, for clean
per-request overhead numbers. For a second capacity pass, edit the
`command:` override commented in `loadtest/docker-compose.loadtest.yml` to
run multiple uvicorn workers, then re-run `just loadtest-up` and repeat the
scenario.

### Database connection pool is also a ceiling

During Task 11's live verification, a `ThroughputUser` run at moderate
concurrency (roughly 20-160 concurrent users) exhausted the gateway's
database connection pool against the single-worker dev stack, well before
any other component saturated. `gatekeep/storage/db.py` currently calls
`create_async_engine(get_settings().database_url, future=True)` with no
pool arguments and `gatekeep/config.py`'s `Settings` exposes no
`database_pool_size` / `database_max_overflow` (or similarly named) field -
so SQLAlchemy's own defaults apply: a `QueuePool` with `pool_size=5` and
`max_overflow=10`, i.e. 15 connections max per gateway process. That is a
real, expected capacity ceiling for `ThroughputUser`/`BreakingPointUser`
runs, not a harness bug.

For a real (non-smoke) capacity pass, raise this alongside the worker
count: since there is no env var for it today, either (a) pass
`pool_size`/`max_overflow` kwargs directly into the `create_async_engine`
call in `gatekeep/storage/db.py` for the duration of the run, or (b) add a
proper `Settings` field first if you want it configurable without editing
source. Either way, remember to also raise Postgres's own `max_connections`
if you push the pool size up materially, and to revert any source edit
before merging - this is a load-testing knob, not a production change.

## Running against a different host

`TARGET_HOST` (read by `loadtest/locustfile.py`) and `LOADTEST_KEYS_PATH`
make the harness host-agnostic - pointing both at a staging deployment
instead of `localhost:8100` is a config change, not new code. Do this only
against a host that also has `LOADTEST_STUB_ENABLED=true` and its own
`loadtest/keys.json` minted against it.
