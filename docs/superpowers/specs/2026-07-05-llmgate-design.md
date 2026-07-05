# llmgate — Self-Hosted LLM Gateway with Prompt-Eval Gating

**Status:** Draft complete, pending user review (2026-07-05)
**Author:** briansia93@gmail.com

## Purpose

A self-hosted gateway that sits in front of Anthropic's API. Real apps route
their LLM traffic through it by changing `base_url`. Its differentiator over a
plain proxy is a **prompt-version regression gate** — prompt changes must pass an
automated eval suite (deterministic checks + Claude-as-judge) before they are
promoted, i.e. "CI for prompts."

Built as a portfolio project to demonstrate, in a single coherent system:
data engineering & cleaning, MLOps & deployment, model evaluation, rate limiting
& cost optimization, and integration with the Claude API.

## Scope Strategy

Ongoing/iterative. Build a **focused v1** as the resume-ready checkpoint, then
layer additional features over time. This doc scopes v1; later iterations are
tracked in the Roadmap section.

## Locked Decisions

| Area | Decision | Rationale |
|------|----------|-----------|
| Language/runtime | Python + FastAPI (async/ASGI) | I/O-bound proxying + streaming; ML ecosystem is Python-first; matches domain conventions (LiteLLM, vLLM server). |
| API surface | OpenAI-compatible `/v1/chat/completions`, translated to Anthropic Messages API | Universal drop-in — any OpenAI SDK/tool points at it via `base_url`. Strongest real-usage story. |
| Persistence | Postgres + Redis, via docker-compose | Postgres = durable state (keys, logs, prompt versions, eval data) + pgvector for embeddings; Redis = rate-limit counters + exact-match cache. Production-standard. |
| Caching | Tiered: exact-hash (Redis) → semantic (embed → pgvector cosine > threshold). Embeddings via a **local `sentence-transformers` model** (`all-MiniLM-L6-v2`), in-process | Demonstrates an embeddings-based ML feature with real cost savings; exact tier avoids wasted compute + false hits. Local model = $0 embedding cost, no second API vendor (Anthropic has no embeddings API), keeps the clone-and-run story dependency-light. |
| Eval gate | Prompt-version regression gate (CLI + CI); scored by deterministic checks + Claude-as-judge rubric; blocks promotion on regression vs baseline. **Judge model configurable, defaults to Haiku 4.5** | Clearest "CI for prompts" narrative; demonstrates model evaluation. Haiku default keeps eval-run cost low (~$0.30 per 50-case run). |
| Eval dataset | Seed set shipped in repo + curation pipeline that mines logged traffic (dedupe incl. semantic near-dupes, PII scrub, error filtering, stratified sampling) | Works out-of-box AND demonstrates data engineering & cleaning + a self-reinforcing loop. |
| Observability | Prometheus metrics + shipped Grafana dashboard; eval results as CLI/HTML report | Industry-standard MLOps signal, cheap to build, visually demoable. |

## Architecture

A single async **FastAPI** gateway service fronting Anthropic's API, backed by
**Postgres** (durable: keys, request logs, prompt versions, eval datasets/results;
pgvector for embeddings) and **Redis** (ephemeral: rate-limit counters,
exact-match cache). Everything comes up with `docker-compose up`: gateway,
Postgres, Redis, Prometheus, Grafana. Two ancillary tools live alongside the
service: a **CLI** (`llmgate ...`) for the eval gate + dataset curation, and a
**GitHub Actions** workflow that runs the eval gate in CI.

```
client (OpenAI SDK, base_url=llmgate)
      │  POST /v1/chat/completions
      ▼
┌─────────────── FastAPI gateway ───────────────┐
│ auth (API key) → rate limit (Redis token      │
│ bucket) → cache lookup (exact→semantic) →      │
│ prompt-version resolve → Anthropic Messages    │
│ API call (stream) → cost accounting → log      │
└───────────────────────────────────────────────┘
   │ writes                     │ exports
   ▼                            ▼
Postgres (logs, pgvector)   Prometheus → Grafana
   │
   ▼ (offline)
curation pipeline → eval dataset → eval gate (CLI/CI)
```

## Components

Each unit has one clear purpose and is independently testable.

- **`api/`** — OpenAI-schema request/response models + a **translation layer**
  (OpenAI ⇄ Anthropic Messages), including streaming (SSE) and error mapping.
- **`providers/anthropic.py`** — thin async client wrapping the Anthropic SDK:
  retries, backoff, timeouts, streaming, token/usage extraction.
- **`middleware/`**
  - `auth` — API-key lookup/validation.
  - `ratelimit` — per-key Redis token bucket.
  - `cache` — tiered: exact hash → semantic via pgvector cosine > threshold.
    Embeddings computed in-process by a local `sentence-transformers` model
    (`all-MiniLM-L6-v2`) — no external embedding vendor.
  - `accounting` — per-request token→cost, emits Prometheus metrics + writes a
    log row.
- **`prompts/`** — prompt registry: versioned prompt templates, an "active"
  pointer per prompt, resolve-at-request-time.
- **`eval/`** — replays an eval dataset against a candidate prompt version;
  scorers = deterministic checks (regex / JSON-schema / exact) + **Claude-as-judge**
  rubric scoring; produces pass/fail vs baseline + HTML/CLI report. Judge model
  is configurable (env/config), defaulting to **Haiku 4.5** to minimize
  eval-run cost; pin the model + low temperature + structured output for
  reproducible scoring.
- **`data/curation.py`** — offline pipeline: pull logged traffic → normalize →
  dedupe (exact + semantic near-dupe) → PII scrub → drop errors/outliers →
  stratified sample → write promotable eval cases.
- **`cli.py`** — `llmgate eval run`, `llmgate prompt promote`,
  `llmgate dataset curate`, etc.
- **`observability/`** — Prometheus exporter + shipped Grafana dashboard JSON.

## Skill Demonstration Mapping

Each target skill maps to concrete, reviewer-visible surface area.

| Target skill | Where it shows up |
|--------------|-------------------|
| **Data engineering & cleaning** | `data/curation.py` pipeline: normalize logged traffic → exact + semantic near-dupe removal → PII scrubbing → error/outlier filtering → stratified sampling → promotable eval cases. Plus the request-logging schema/write path that feeds it. |
| **MLOps & deployment** | Full stack via `docker-compose` (gateway, Postgres, Redis, Prometheus, Grafana); versioned prompts with an explicit promotion workflow; GitHub Actions running the eval gate as CI; shipped Grafana dashboard. |
| **Model evaluation** | `eval/` regression gate: replay dataset against a candidate prompt version, score with deterministic checks + Claude-as-judge rubric, compare vs baseline, block promotion on regression, emit HTML/CLI report. |
| **Rate limiting & cost optimization** | Per-key Redis token bucket; tiered exact+semantic cache (measurable cost savings via cache-hit rate); per-request token→cost accounting surfaced in Grafana. |
| **Claude API integration** | `providers/anthropic.py` (async Anthropic SDK wrapper: retries, backoff, timeouts, streaming, usage extraction) + the OpenAI⇄Anthropic translation layer; Claude also powers the judge in the eval gate. |

## v1 Scope Boundary (in / out)

**In v1 (the resume-ready checkpoint):**

- OpenAI-compatible `POST /v1/chat/completions`, both non-streaming and streaming (SSE).
- Translation layer: OpenAI request/response ⇄ Anthropic Messages, including error mapping.
- `providers/anthropic.py`: async Anthropic client with retries, backoff, timeouts, streaming, usage extraction.
- API-key auth (static keys in Postgres).
- Per-key rate limiting (Redis token bucket).
- Tiered cache: exact-hash (Redis) → semantic (embed → pgvector cosine > threshold).
- Per-request cost accounting + durable request logging.
- Prompt registry: versioned templates, per-prompt "active" pointer, `promote` workflow.
- Eval gate: replay dataset against a candidate version, deterministic + Claude-as-judge scoring, baseline comparison, blocks on regression; HTML/CLI report.
- Seed eval dataset shipped in repo + `data/curation.py` traffic-mining pipeline.
- Prometheus metrics + shipped Grafana dashboard, all in docker-compose.
- GitHub Actions workflow running the eval gate.
- Unit + integration tests (see Testing Strategy).

**Out of v1 (deferred to Roadmap):**

- Additional upstream providers (OpenAI, Google) — Anthropic only in v1.
- Anthropic-native `/v1/messages` surface — OpenAI-compat only in v1.
- Eval-driven model-downgrade routing (Haiku vs Sonnet).
- Canary / gradual prompt rollout with live auto-rollback.
- Custom web dashboard UI (Grafana covers v1).
- Multi-tenant/org management, billing portal, budgets with hard caps.
- Auth beyond static API keys (OAuth, key rotation UI).
- HA / multi-node distributed concerns beyond what Redis/Postgres give for free.

## Testing Strategy

- **Unit tests** — translation layer (golden OpenAI↔Anthropic fixtures, incl. streaming chunks and error mapping); rate limiter (bucket refill/exhaustion); cache (exact key derivation, semantic threshold boundaries); cost calculation; curation transforms (dedupe, PII scrub, stratified sampling) on fixture data; scorers (deterministic checks + judge-response parsing).
- **Integration tests** — gateway end-to-end against a **mocked Anthropic client** but **real Postgres + Redis** (docker-compose or testcontainers); covers auth → rate limit → cache → accounting → log write, and the streaming path.
- **Eval-gate self-test** — a fixture dataset plus a known-good and a known-bad prompt version, asserting the gate *passes* the good one and *blocks* the bad one. This proves the differentiator actually works.
- **Judge reliability** — pin the judge model, low temperature, structured/JSON output; test that rubric parsing is robust to formatting variance.
- **CI** — GitHub Actions runs unit + integration suites, then runs the eval gate against the seed dataset on every PR.

## Cost Model

Infrastructure and CI are free; the only recurring cost is Anthropic API calls,
dominated by the eval gate.

- **Infra + CI: $0.** GitHub Actions (free tier covers the few-minute eval job),
  Prometheus, Grafana, Postgres, Redis, and the gateway are all open-source and
  self-hosted via docker-compose. Anyone who clones the repo runs the full stack
  locally at no cost (bringing their own Anthropic API key).
- **Embeddings: $0.** Local `sentence-transformers` model — no embedding vendor.
- **Claude API — the only real cost.** Driven by eval runs (candidate responses +
  Claude-as-judge). At ~50 eval cases, a full run is ~$0.30 with a Haiku judge
  (Haiku 4.5 $1/$5 per 1M in/out; Sonnet 5 intro $2/$10 through 2026-08-31).
  Running the gate on every PR during active development stays in the
  single-digit-dollars-per-month range. Plus incidental cost from manual test
  traffic through the gateway.
- **Realistic total while building v1: under ~$10/month.** $0 for a third party
  who clones it and supplies their own key.

## Roadmap (post-v1 iterations)

1. **Anthropic-native surface** — add `/v1/messages` passthrough so both OpenAI and Anthropic SDK users can point at the gateway.
2. **Multi-provider upstream** — OpenAI + Google as additional backends with provider-agnostic routing.
3. **Model-downgrade routing gate** — eval-driven decision to route "easy" requests to Haiku vs Sonnet, only when quality holds.
4. **Canary rollout** — gradual prompt-version rollout with automatic rollback on live metric/quality regression.
5. **Custom web dashboard** — a first-party UI for cost/usage/eval history beyond Grafana.
6. **Budgets & quotas** — per-key spend caps with hard limits + alerting.
7. **Richer eval** — async batch eval, larger curated datasets, and a human-in-the-loop labeling UI for promoting eval cases.
