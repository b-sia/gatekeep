# Gatekeep

Self-hosted, OpenAI-compatible LLM gateway with prompt-eval gating.

Point your app at Gatekeep instead of OpenAI/Anthropic directly. It authenticates
requests with its own API keys and routes them to the configured provider (Claude,
OpenAI, Google, local Ollama models), so you get one stable interface regardless of
which model is behind it - plus rate limiting, response caching, per-key spend
budgets, cost accounting, and an analytics dashboard.

```
Your App -> Gatekeep (auth + routing) -> Provider (Claude, OpenAI, Google, Ollama)
```

## Prerequisites

- Docker and Docker Compose - runs the gateway plus Postgres (with `pgvector`),
  Redis, Ollama, Prometheus, and Grafana.
- An `ANTHROPIC_API_KEY`. Other provider keys are optional.
- For development only: Python 3.11+, `psql`, and Node.js 20+ (dashboard SPA).

## Installation

1. Copy the environment template and fill in your provider credentials:
   ```bash
   cp .env.example .env
   ```

2. Start the gateway and its dependencies:
   ```bash
   docker-compose up -d
   ```

3. Create an API key for calling the gateway:
   ```bash
   bash scripts/init-test-key.sh
   ```
   This prints a raw key like `gk-...` - save it, it is only shown once.

The gateway now listens on `http://localhost:8100`, with the dashboard at
`http://localhost:8100/dashboard`.

## Usage

Send an OpenAI-shaped chat completion request:

```bash
curl -X POST http://localhost:8100/v1/chat/completions \
  -H "Authorization: Bearer gk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

Or from the OpenAI client library, by pointing `base_url` at the gateway:

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

Streaming (`"stream": true`) and non-streaming requests are both supported.
`demo/` is a runnable chat app that exercises the gateway the way a real client
would; `demo/example_client.py` has standalone versions of the common integration
patterns.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible chat completions |
| `POST /v1/messages` | Native Anthropic Messages API shape |
| `GET /dashboard` | Analytics UI (cost, usage, latency, prompts, evals) |
| `GET /metrics` | Prometheus scrape endpoint (unauthenticated) |
| `GET /healthz` | Liveness check (unauthenticated) |

The `gatekeep` CLI manages prompt templates, eval suites, and key budgets. Run
`gatekeep --help` for the full reference.

Deeper documentation lives next to the code it describes:

- [`gatekeep/providers/`](gatekeep/providers/README.md) - model routing, provider
  prefixes, and the `/v1/messages` endpoint
- [`gatekeep/middleware/`](gatekeep/middleware/README.md) - rate limiting, caching,
  and spend budgets
- [`gatekeep/observability/`](gatekeep/observability/README.md) - metrics, latency
  semantics, and Grafana
- [`dashboard/`](dashboard/README.md) - the analytics SPA
- [`demo/`](demo/README.md) - the example chat app
- [`prompts/`](prompts/README.md) - prompt templates, the eval gate, and cost routing

## Configuration

All settings are read from the environment or a local `.env` file. `.env.example`
is the authoritative list; the essentials are:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | - | Postgres DSN, async driver (`postgresql+asyncpg://...`) |
| `TEST_DATABASE_URL` | - | Separate database for the test suite; must differ from `DATABASE_URL` |
| `REDIS_URL` | - | Redis DSN, used for the exact cache, rate limits, and budgets |
| `ANTHROPIC_API_KEY` | - | Key the gateway uses to call Claude |
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` | none | Only needed for `openai/` or `google/` prefixed models |
| `OLLAMA_HOST` | `http://localhost:11434` | Local Ollama address |
| `DEFAULT_MODEL` | `claude-sonnet-5` | Model the `gatekeep eval` CLI generates with when none is given |
| `DEFAULT_MAX_TOKENS` | `4096` | Completion cap when the client sends none |
| `MODEL_ALIASES` | see `config.py` | JSON map of client-facing model names to Claude models |
| `EVAL_JUDGE_MODEL` | `claude-sonnet-5` | Judge model for `llm_judge` eval checks |
| `EVAL_PASS_THRESHOLD_DEFAULT` | `0.9` | Default pass threshold for a new eval suite |

Rate-limit, cache, and budget-alert tuning are documented in
[`gatekeep/middleware/README.md`](gatekeep/middleware/README.md).

## Testing

Install the package locally so `pytest` and the CLI run against your editor's
Python:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Tests need a live Postgres (with `pgvector`) and Redis - the same services
`docker-compose` provides. They run against `TEST_DATABASE_URL`, **not**
`DATABASE_URL`: each test drops and recreates the whole schema, so `conftest.py`
refuses to run if the two point at the same database. The test database is created
automatically if it does not exist.

```bash
docker-compose up -d postgres redis   # dependencies only, no gateway image build
pytest
```

Prompt template changes are gated by CI rather than pytest - see
[`prompts/README.md`](prompts/README.md).

## Deployment

The gateway ships as a container. `Dockerfile` is a two-stage build - Node builds
the dashboard SPA, then the Python image installs the package, bakes the
semantic-cache embedding weights in so a fresh container never fetches them at
runtime, and copies the built SPA in. It serves on port 8000, which
`docker-compose.yml` maps to 8100 on the host.

The container's entrypoint runs `alembic upgrade head` before starting uvicorn, so
migrations are applied on every deploy automatically. Roll out one instance at a
time if a migration is not backward-compatible with the running version.

For a real deployment, supply your own managed Postgres (with the `pgvector`
extension) and Redis via `DATABASE_URL` / `REDIS_URL`; the Compose file's Postgres,
Redis, Ollama, Prometheus, and Grafana services are development conveniences.

Point your orchestrator's liveness probe at `GET /healthz` and your Prometheus at
`GET /metrics`. Both are unauthenticated, so keep them off the public internet.

Schema changes are authored as Alembic revisions:

```bash
alembic revision --autogenerate -m "add a column"   # after editing gatekeep/storage/models.py
```

## Project layout

```
gatekeep/           gateway source
  app.py            FastAPI app and request paths
  api/              request/response schemas, translation, dashboard API
  providers/        per-provider adapters behind a common interface
  middleware/       auth, rate limiting, caching, budgets
  observability/    Prometheus metrics and Grafana provisioning
migrations/         Alembic database migrations
prompts/            versioned prompt templates and their eval fixtures
dashboard/          React dashboard SPA, served at /dashboard
demo/               example chat app showing gateway integration
scripts/            setup helpers (init-test-key.sh, run-demo.sh)
tests/              pytest suite, one file per module
.github/workflows/  CI, including the prompt eval gate
```

Design specs, implementation plans, and the roadmap are kept off `master` on the
`docs/design-archive` branch.

## Contributing

Issues and pull requests are welcome.

- Python is linted and formatted with `ruff`; run `ruff check .` and
  `ruff format .` before opening a PR.
- Every change needs passing tests. Add coverage in `tests/` alongside the code.
- Changes under `prompts/` additionally run the `eval-gate` workflow, which scores
  the prompt's eval suite against the diff. See
  [`prompts/README.md`](prompts/README.md).
- Keep documentation next to the code it describes; the root README stays a short
  entry point.

## License

MIT - see [LICENSE](LICENSE).
