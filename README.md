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

`/metrics` is a Prometheus-format endpoint (unauthenticated, like `/healthz`); `docker-compose.yml` also runs Prometheus and a Grafana dashboard at `http://localhost:3000` for cost, usage, and cache-hit-rate visualization.

Prompt templates registered via the `gatekeep prompt` CLI (`gatekeep prompt create/promote/rollback ...`) are cache-aware: promoting a new prompt version automatically invalidates any cached responses that were built using the old version, so clients never see a stale answer generated from a prompt that's no longer active.

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
model. The substitution is recorded in `request_logs.routed_from`.

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
demo/           example chat app showing gateway integration
docs/           design docs and specs
```
