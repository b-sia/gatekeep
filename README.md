# Gatekeep

Self-hosted, OpenAI-compatible LLM gateway with prompt-eval gating.

Point your app at Gatekeep instead of OpenAI/Anthropic directly. It authenticates requests with its own API keys and routes them to the configured provider (Claude, local Ollama models, etc), so you get one stable interface regardless of which model is behind it.

```
Your App -> Gatekeep (auth + routing) -> Provider (Claude, Ollama, ...)
```

## Setup

1. Copy the environment template and fill in your provider credentials:
   ```bash
   cp .env.example .env
   ```

2. Start the gateway and its dependencies (Postgres, Redis, Ollama):
   ```bash
   docker-compose up -d
   ```

3. Create an API key for calling the gateway:
   ```bash
   bash scripts/init-test-key.sh
   ```
   This prints a raw key like `gk-...` - save it, it's only shown once.

The gateway now listens on `http://localhost:8100`.

## Develop on Gatekeep

The gateway itself runs in Docker (see Setup above), but for local development you'll
usually want the `gatekeep` package installed directly so you can run tests and the
CLI against your editor's Python.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Tests need a live Postgres (with the `pgvector` extension) and Redis - the same
services `docker-compose up -d` already gives you - and read `DATABASE_URL` /
`REDIS_URL` from `.env`. Each test drops and recreates the schema, so point
`DATABASE_URL` at a throwaway database, not one with data you care about.

```bash
docker-compose up -d postgres redis   # dependencies only, no need to build the gateway image
pytest
```

Database schema changes go through Alembic:

```bash
alembic upgrade head                              # apply migrations
alembic revision --autogenerate -m "add a column"  # generate a new one after editing gatekeep/models.py
```

Project layout, in more detail than the summary at the bottom of this file:

| Path | What's there |
|---|---|
| `gatekeep/app.py` | FastAPI app: `/v1/chat/completions`, `/healthz`, `/metrics` |
| `gatekeep/providers/` | Per-provider adapters (Anthropic, Ollama) behind a common interface |
| `gatekeep/middleware/` | Rate limiting, exact/semantic caching |
| `gatekeep/prompts.py`, `evals.py`, `curation.py`, `fixtures.py` | Prompt versioning, eval suites, real-traffic curation, CI fixtures - see the "Eval gate" section below |
| `gatekeep/cli.py` | The `gatekeep prompt ...` / `gatekeep eval ...` commands, run via `python -m gatekeep.cli` or the installed `gatekeep` console script |
| `gatekeep/observability/` | Prometheus metric definitions, plus the ops Grafana dashboard `docker-compose.yml` provisions |
| `dashboard/` | First-party React/TypeScript dashboard SPA, served by the gateway at `/dashboard` - the analytics surface for cost, usage, and latency; see "Dashboard" below |
| `demo/` | Standalone chat app exercising the gateway as a real client would |
| `tests/` | Pytest suite, one file per module; `conftest.py` resets the DB schema per test |

Run `gatekeep --help`, `gatekeep prompt --help`, and `gatekeep eval --help` for the
full CLI reference - `prompt` covers template versioning/promotion/rollback, `eval`
covers suite/case management and running suites manually.

Prompt template changes are a special case: they live under `prompts/` and go
through the `eval-gate` CI workflow (`.github/workflows/eval-gate.yml`) rather than
regular pytest. See `prompts/README.md` and the "Eval gate" section below.

## Test it with curl

```bash
curl -X POST http://localhost:8100/v1/chat/completions \
  -H "Authorization: Bearer gk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

Swap `"model"` for any model your configured provider(s) support, e.g. `llama3.2` if you're running the Ollama service from `docker-compose.yml`.

### Routing to OpenAI or Google directly

Bare model names (`gpt-4`, `llama3.2`, ...) route through the alias table to
Claude, or fall through to Ollama - this keeps the zero-config demo working
without extra API keys. To send a request to the real OpenAI or Google API
instead, prefix the model with `openai/` or `google/`:

```json
{"model": "openai/gpt-4o", "messages": [...]}
{"model": "google/gemini-flash-latest", "messages": [...]}
```

Requires `OPENAI_API_KEY` / `GOOGLE_API_KEY` to be set; requests to a
prefixed provider with no key configured fail with an upstream auth error.

### Native Anthropic SDK support: `/v1/messages`

Clients using the `anthropic` SDK directly (rather than an OpenAI-compatible
client) can point `base_url` at gatekeep and use `POST /v1/messages` with the
real Anthropic Messages API request/response shape - no OpenAI translation
involved. It shares auth, rate limiting, the tiered cache, `prompt_name`/
`route_by_cost` prompt-registry and cost-routing extensions, and cost
accounting with `/v1/chat/completions`; a cached response from either
endpoint can be served by the other.

Known limitation: gatekeep's internal `CompletionResult` only carries
OpenAI-canonical stop reasons, so an Anthropic `stop_sequence` hit and a
plain `end_turn` are indistinguishable at this layer - both are reported as
`end_turn` on `/v1/messages` responses.

## Rate limiting, caching, and cost tracking

Every key is rate-limited by a per-minute token bucket (`rate_limit_tokens_per_min`, default 100). Once a key's bucket is empty, the gateway returns a 429 with a `Retry-After` header instead of forwarding the request:

```bash
curl -i http://localhost:8100/v1/chat/completions \
  -H "Authorization: Bearer gk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}'
# HTTP/1.1 429 Too Many Requests
# Retry-After: 3
```

Non-streaming requests are checked against two caches before hitting the provider:

- **Exact cache** (Redis): identical requests (same model, messages, `max_tokens`, etc.) are served straight back within the TTL set by `cache_exact_ttl_seconds` (default 7 days).
- **Semantic cache** (Postgres + pgvector): requests whose embedding is more similar than `semantic_cache_similarity_threshold` (default 0.95 cosine similarity) to a previously-cached request are served the cached answer, even with different wording.

Both kinds of cache hit skip the provider call entirely and are logged with `cached: true` in `request_logs`, so cache-hit rate and cost savings show up in cost accounting the same way normal requests do.

Every request - cached or not - is logged to `request_logs` with token counts and USD cost, and exposed via:

```bash
curl http://localhost:8100/metrics | grep gatekeep_cache_exact_hits
```

`/metrics` is a Prometheus-format endpoint (unauthenticated, like `/healthz`)
and is the **integration surface**: scrape it into whatever Prometheus you
already run. `docker-compose.yml` also brings up Prometheus and a small
"Gatekeep - Ops" Grafana dashboard at `http://localhost:3000` as a
local-development convenience, scoped to the signals Postgres structurally
cannot serve - rate-limit rejections, budget alerts, and real-time latency
tails. Cost, savings, cache-hit rate, and latency attribution live on
`/dashboard`, which computes them exactly from `request_logs` rather than
extrapolating from histogram buckets.

Latency metrics:

- `gatekeep_request_duration_seconds{model,path}` - end-to-end latency, the
  full request span on every `path`, recorded in one place. `path` is one of
  `cache_exact`, `cache_semantic`, `provider`, `stream`. Aggregating across
  paths is valid, but the distributions differ by orders of magnitude, so a
  pinned `path` is usually the more useful query.
- `gatekeep_time_to_last_token_seconds{model}` - request start until the last
  streamed token, streaming only. Smaller than
  `gatekeep_request_duration_seconds{path="stream"}` for the same request,
  which also covers the trailing SSE events and response teardown.
- `gatekeep_provider_duration_seconds{model}` - time in the upstream call. On
  the streaming path this includes downstream backpressure, since the stream
  loop is pull-based, so it is not comparable like-for-like with the
  non-streaming figure.
- `gatekeep_gateway_overhead_seconds{model,path}` - request time not spent
  upstream, computed by the same middleware as `request_duration_seconds`
  from the same span, so `overhead = duration - provider` holds exactly on
  every path. On a cache hit this is the entire duration.
- `gatekeep_ttft_seconds{model}` - time to first token, streaming only.
- `gatekeep_inter_token_seconds{model}` - gap between streamed deltas. This is
  really inter-*chunk* latency: providers do not guarantee one token per delta.
  The token-normalized figure is derived from `request_logs` instead, as
  `(duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0)`, which is
  undefined below two completion tokens.

Per-request latency is also stored on `request_logs` as `duration_ms`,
`provider_ms`, `ttft_ms`, and `path`. `provider_ms` is NULL on a cache hit and
`ttft_ms` is NULL on any non-streamed request. A NULL `provider_ms` alone
cannot distinguish a cache hit from a row predating the migration - filter on
`cached`. `path` carries the same four values as the Prometheus `path` label
(`cache_exact`, `cache_semantic`, `provider`, `stream`), each sourced from
its own module-level constant in `gatekeep/app.py` rather than a repeated
string literal - a `_finish_request` parameter carries the constant into
both `mark()` and `log_request()` on the non-streaming paths, and the
streaming path's `mark()` call and SSE-generator `log_request()` call both
read `_STREAM_PATH` directly, since they run in two different functions
with no shared parameter to carry it through. Either way the metric label
and the DB column cannot drift apart from a typo in either. It is NULL only
on rows predating migration `0012`, which every latency query excludes.

`duration_ms` means two different things depending on `path`: end-to-end on
the non-streaming paths, and time-to-last-token on `stream`. Percentiles are
never blended across the two.

Prompt templates registered via the `gatekeep prompt` CLI (`gatekeep prompt create/promote/rollback ...`) are cache-aware: promoting a new prompt version automatically invalidates any cached responses that were built using the old version, so clients never see a stale answer generated from a prompt that's no longer active.

## Dashboard

Once the gateway is running, the first-party dashboard is served at
`http://localhost:8100/dashboard`. It is the **analytics surface**: cost,
usage, cache savings, latency (end-to-end, provider, gateway overhead, and
TTFT, with end-to-end broken down by path, model, key, and prompt), prompt
version history, and eval history - all read from `request_logs` and the
prompt/eval tables, filterable by model and time window. Per-key and
per-prompt latency attribution lives here rather than in Prometheus because
`key_id` is deliberately not a metric label: the wide latency bucket set
means adding it would push the per-key series count roughly two orders of
magnitude higher.

On first load it prompts for an API key (the same kind used for
`/v1/chat/completions`); the key is stored in the browser's `localStorage`
and sent as a bearer token to the dashboard's own read-only API under
`/dashboard/api/*`.

One caveat worth knowing before comparing the two surfaces: `/dashboard`
reads slightly lower than Grafana for identical traffic. `request_logs.duration_ms`
stops just before the accounting write, so it excludes JSON serialization and
the socket write, where `gatekeep_request_duration_seconds` covers the full
ASGI span.

For local frontend development, run the dev server separately from the
gateway:

```bash
cd dashboard && npm install && npm run dev
```

This runs Vite's dev server with hot reload, proxying `/dashboard/api`
requests to `http://localhost:8100` (so the gateway still needs to be up via
`docker-compose up -d`).

## Run the demo app

`demo/` contains a small chat web app that shows Gatekeep used the way a real client would - not just a single curl call.

```bash
export GATEKEEP_API_KEY=gk-your-key   # from init-test-key.sh above
python demo/app.py
```

Open `http://localhost:8200` and chat. Use the model dropdown to switch between providers, and toggle streaming on/off. The page's "How this works" section walks through the same integration examples as below.

Env vars the demo app reads (also loaded from `.env` if present):

| Variable | Default | Purpose |
|---|---|---|
| `GATEKEEP_URL` | `http://localhost:8100` | Address of the gateway |
| `GATEKEEP_API_KEY` | none (required) | Key created via `init-test-key.sh` |
| `DEFAULT_MODEL` | `claude-sonnet-5` | Model used when none is specified |

`demo/example_client.py` has runnable, standalone versions of the integration patterns below (basic request, streaming, retries, multi-turn, provider switching) if you want to see them outside the web UI.

## Eval gate and prompt quality control

Prompt templates live in-repo under `prompts/` (one `*.txt` per prompt, named by
filename). A change is a PR: the diff gets reviewed and the `eval-gate` workflow
runs the prompt's eval suite against the change.

Register a suite and cases, then curate more from real traffic:

```bash
# One suite per prompt; threshold defaults to EVAL_PASS_THRESHOLD_DEFAULT.
gatekeep eval create-suite system-context --threshold 0.9

# Add a deterministic case from a JSON messages file.
echo '[{"role":"user","content":"ping"}]' > /tmp/case.json
gatekeep eval add-case system-context --input-file /tmp/case.json \
  --check-type contains --expected pong

# Grow the suite from recent real traffic (writes unreviewed cases).
gatekeep eval curate system-context --limit 20
gatekeep eval review system-context   # approve/reject each, interactively

# Run the suite manually (defaults to the active version + DEFAULT_MODEL).
gatekeep eval run system-context
```

Promotion is gated: `gatekeep prompt promote <name> <version>` runs the suite
first and refuses to activate a version that scores below the suite threshold,
printing a per-case report. Prompts with no suite promote exactly as before
(the gate is opt-in). `rollback` is never gated.

The `llm_judge` check grades output with a fixed, stronger judge model
(`EVAL_JUDGE_MODEL`, default `claude-sonnet-5`) rather than the model under
test, to avoid a model rubber-stamping its own failure mode.

CI requires an `ANTHROPIC_API_KEY` repository secret so the gate's generation
and judge calls can reach the provider.

### Cost-based routing (opt-in)

Send `"route_by_cost": true` (optionally with `"quality_floor": 0.9`) alongside
`"prompt_name"` to let the gateway substitute the cheapest model that has a
passing eval run at or above the floor for that prompt. It never overrides an
explicit model choice unless you opt in, and never routes up to a costlier
model. For non-streaming requests, the substitution is recorded in
`request_logs.routed_from`; streaming requests are routed the same way but do
not currently record `routed_from` (a known Phase 3 limitation).

## Integrate Gatekeep into your own app

**Using the OpenAI client library** (recommended if you already use it):

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key="gk-your-key",              # a Gatekeep key, not an OpenAI one
    base_url="http://localhost:8100/v1",
)

response = await client.chat.completions.create(
    model="claude-sonnet-5",
    messages=[{"role": "user", "content": "Hello!"}],
)
```

**Using httpx directly:**

```python
import httpx

async with httpx.AsyncClient() as client:
    response = await client.post(
        "http://localhost:8100/v1/chat/completions",
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "Hello!"}],
        },
        headers={"Authorization": "Bearer gk-your-key"},
    )
```

Both streaming (`"stream": true`) and non-streaming requests are supported, matching the OpenAI chat completions API shape.

## Project layout

```
gatekeep/       gateway source (FastAPI app, providers, middleware)
migrations/     Alembic database migrations
scripts/        setup helpers (init-test-key.sh, run-demo.sh)
dashboard/      first-party React dashboard SPA, served at /dashboard
demo/           example chat app showing gateway integration
docs/           design docs and specs
```
