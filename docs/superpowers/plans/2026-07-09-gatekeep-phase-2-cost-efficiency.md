# gatekeep Phase 2 — Cost Efficiency & Prompt Management

**Goal:** Add cost optimization, caching, and prompt versioning on top of the Phase 1 gateway core. Enable users to control API spend and manage prompt evolution.

**Key deliverables:**
- Per-key rate limiting (Redis token bucket)
- Tiered caching: exact-hash (Redis) + semantic (pgvector + embeddings)
- Per-request cost accounting & durable logging
- Prometheus metrics export
- Grafana dashboard (cost, usage, cache hit rate)
- Prompt registry: versioned templates with active pointers
- CLI tools for prompt management

**Tech additions:** `sentence-transformers`, Prometheus Python client

---

## Rationale

After Phase 1, the gateway proxies requests end-to-end but has no visibility or control over cost. Phase 2 adds:

1. **Rate limiting** — prevent runaway spend per key
2. **Caching** — avoid redundant API calls
   - Exact-match for identical requests (fast, Redis-backed)
   - Semantic matching for similar requests (pgvector cosine similarity via local embeddings)
3. **Cost accounting** — log every request with token counts and cost; export metrics for dashboards
4. **Prompt management** — versioned prompt templates; promote-workflow to manage active versions

These are foundational for Phase 3 (the eval gate), which will gate prompt promotion on regression tests.

---

## Architecture Changes

```
client → [auth] → [rate limit] → [cache lookup] → [translate]
           ↓           ↓            ↓ (exact/semantic)
         DB key      Redis         Redis + pgvector
                                   embeddings (in-process)
                     [Anthropic API] → [cost accounting] → [logging]
                                              ↓                 ↓
                                         Prometheus        Postgres
                                             ↓                 ↓
                                          Grafana          request_logs
```

New tables in Postgres:
- `request_logs` — every request: timestamp, key_id, model, prompt_tokens, completion_tokens, cost, cached, cache_hit, response_id
- `prompts` — versioned prompt templates: name, version, template, active (bool), created_at
- `prompt_versions` — history of version changes: prompt_id, version_num, template_text, created_by, notes

New Redis structures:
- `cache:exact:{hash}` — cached responses (expires based on config)
- `ratelimit:{key_id}:tokens` — current token count in bucket (sliding window)

---

## Phase 2 Tasks

### Task 1: Rate Limiting Middleware

**Files:**
- Modify: `gatekeep/middleware/ratelimit.py` (new)
- Test: `tests/test_ratelimit.py`
- Modify: `gatekeep/app.py` to apply middleware

**Interfaces:**
- Consumes: `gatekeep.config.Settings` (add fields: `rate_limit_tokens_per_min`, `rate_limit_refill_rate`)
- Consumes: `gatekeep.db.get_session` (lookup key config)
- Produces: FastAPI dependency `require_rate_limit(key: ApiKey) -> ApiKey` that raises `HTTPException(429)` if quota exhausted

**Behavior:**
- Per-key token bucket in Redis
- Token bucket: capacity = N tokens/min, refill = 1 per `1000/N` ms
- On each request, check available tokens; if insufficient, raise 429
- Emit metric: `gatekeep_rate_limit_remaining{key_id=...}`

**Implementation notes:**
- Use Redis `INCR` + `EXPIRE` for simple sliding-window rate limiting (or `ZADD` for stricter leaky bucket)
- Return `Retry-After` header on 429
- Default: 100 tokens/min per key (configurable per key in API table)

---

### Task 2: Request Logging & Cost Accounting

**Files:**
- Create: `gatekeep/models.py` additions: `RequestLog` table
- Create: `gatekeep/accounting.py` — token→cost calculation + log writing
- Test: `tests/test_accounting.py`
- Modify: `gatekeep/app.py` to log every response

**Interfaces:**
- Consumes: provider result (token counts from Anthropic)
- Produces: `RequestLog` rows + Prometheus metrics
  - `gatekeep_request_tokens{model,key_id}` (histogram: prompt + completion tokens)
  - `gatekeep_request_cost_usd{model,key_id}` (histogram: USD cost per request)
  - `gatekeep_cache_hit_rate{model}` (gauge: cache hit % over last N requests)

**RequestLog schema:**
```python
class RequestLog(Base):
    __tablename__ = "request_logs"
    
    id: int (pk)
    created_at: datetime
    key_id: int (fk ApiKey)
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    cached: bool  # was this a cache hit?
    cache_key: str (nullable)  # exact hash if applicable
    response_id: str  # for tracing
```

**Cost model:**
- Hardcoded per-model pricing (read from env or constants)
- Example: `claude-sonnet-5: (input=$2/$1M, output=$10/$1M)`
- Calculate: `(prompt_tokens / 1M * input_price) + (completion_tokens / 1M * output_price)`

---

### Task 3: Exact-Match Cache (Redis)

**Files:**
- Create: `gatekeep/middleware/cache_exact.py`
- Test: `tests/test_cache_exact.py`
- Modify: `gatekeep/app.py` to check cache before calling Anthropic

**Interfaces:**
- Consumes: `ChatCompletionRequest`
- Produces: cached `ChatCompletionResponse` or None
  - `gatekeep_cache_exact_hits{model}` (counter)
  - `gatekeep_cache_exact_misses{model}` (counter)

**Behavior:**
- Hash request: SHA256(model + sorted messages + stop sequences)
- Redis key: `cache:exact:{hash}`
- Value: JSON-serialized `ChatCompletionResponse`
- TTL: configurable, default 7 days
- Invalidate on: prompt update (Task 5), manual clear

**Implementation:**
- Deterministic hashing of request (sort keys for consistency)
- Skip caching if response contains tools/function calls (Phase 3+)
- Log cache hit in `RequestLog.cached = true`

---

### Task 4: Semantic Cache (pgvector + embeddings)

**Files:**
- Create: `gatekeep/embeddings.py` — local sentence-transformers wrapper
- Create: `gatekeep/middleware/cache_semantic.py`
- Modify: `gatekeep/models.py` — add `CachedResponse` table with `embedding` (vector)
- Test: `tests/test_cache_semantic.py`, `tests/test_embeddings.py`
- Modify: `gatekeep/app.py` to check semantic cache if exact miss

**Interfaces:**
- Consumes: `ChatCompletionRequest`
- Produces: similar cached response if similarity > threshold
  - `gatekeep_cache_semantic_hits{model}` (counter)
  - `gatekeep_cache_semantic_misses{model}` (counter)
  - `gatekeep_cache_semantic_similarity{model}` (histogram: max similarity found)

**CachedResponse schema:**
```python
class CachedResponse(Base):
    __tablename__ = "cached_responses"
    
    id: int (pk)
    created_at: datetime
    exact_hash: str (unique)  # link to exact cache
    user_messages_text: str  # concatenate user messages for embedding
    embedding: Vector  # pgvector
    response_text: str
    model: str
    cost_usd: float
```

**Behavior:**
- On cache miss (exact), embed user messages using local `all-MiniLM-L6-v2`
- Query pgvector: find rows where cosine_similarity > threshold (default 0.95)
- Return highest-similarity cached response (with disclaimer/caveat in metadata)
- If semantic hit used, log: `RequestLog.cached = true, cache_key = "semantic"`
- TTL: same as exact cache (invalidate together)

**Embeddings setup:**
- Load model on startup (cached in memory)
- Embed only user+system messages (not assistant)
- Skip embedding if messages are too long (> 1000 tokens)

---

### Task 5: Prompt Registry & Versioning

**Files:**
- Modify: `gatekeep/models.py` — add `Prompt`, `PromptVersion` tables
- Create: `gatekeep/prompts.py` — registry logic
- Create: `gatekeep/cli.py` — `gatekeep prompt` commands
- Test: `tests/test_prompts.py`
- Modify: `gatekeep/app.py` — resolve active prompt at request time

**Interfaces:**
- Consumes: `gatekeep.db.SessionLocal`
- Produces:
  - `get_prompt(name: str, session) -> str` — fetch active template
  - CLI: `gatekeep prompt create <name> <template_file>`
  - CLI: `gatekeep prompt list`
  - CLI: `gatekeep prompt show <name>`
  - CLI: `gatekeep prompt promote <name> <version>`
  - CLI: `gatekeep prompt rollback <name>`

**Prompt schema:**
```python
class Prompt(Base):
    __tablename__ = "prompts"
    
    id: int (pk)
    name: str (unique) — e.g., "system-context", "user-instruction"
    active_version: int (fk PromptVersion)
    created_at: datetime
    updated_at: datetime


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    
    id: int (pk)
    prompt_id: int (fk Prompt)
    version_num: int (auto-increment per prompt)
    template: str (Jinja2 template, or plain string)
    created_at: datetime
    created_by: str (nullable — for audit)
    notes: str (nullable — changelog)
    active: bool (denormalized for fast lookup)
```

**Behavior:**
- Prompts are defined as templates (plain strings for Phase 2, Jinja2 support in Phase 3)
- Request resolution: if request includes `{prompt_name: "system-context", ...}`, substitute active version
- Promotion: atomically update `Prompt.active_version` (Phase 3 eval gate will guard this)
- Rollback: revert to previous version (one-click recovery)

**CLI examples:**
```bash
gatekeep prompt create system-context my-system-prompt.txt
gatekeep prompt show system-context  # show active version
gatekeep prompt promote system-context 2  # promote v2
gatekeep prompt rollback system-context  # revert to previous
```

---

### Task 6: Prometheus Exporter & Grafana Dashboard

**Files:**
- Create: `gatekeep/observability/metrics.py` — Prometheus metric definitions
- Create: `gatekeep/observability/grafana.json` — pre-built dashboard JSON
- Modify: `gatekeep/app.py` — expose `/metrics` endpoint
- Modify: `docker-compose.yml` — add Prometheus + Grafana services

**Metrics:**
- `gatekeep_requests_total{model, key_id}` — total requests
- `gatekeep_request_tokens{model, key_id}` — histogram of tokens per request
- `gatekeep_request_cost_usd{model, key_id}` — histogram of USD cost
- `gatekeep_rate_limit_remaining{key_id}` — tokens remaining in bucket
- `gatekeep_cache_exact_hits{model}` — exact cache hits (counter)
- `gatekeep_cache_exact_misses{model}` — exact cache misses (counter)
- `gatekeep_cache_semantic_hits{model}` — semantic cache hits (counter)
- `gatekeep_cache_semantic_similarity{model}` — max similarity of semantic hit (histogram)
- `gatekeep_cache_cost_saved_usd` — cumulative cost saved by caching

**Grafana Dashboard panels:**
1. **Cost per key** — stacked bar, last 7 days
2. **Cache hit rate** — line graph, exact vs semantic
3. **Avg tokens per request** — line + histogram
4. **Rate limit exhaustions** — count over time
5. **Cached cost savings** — cumulative USD saved

**Integration:**
- Prometheus scrapes `/metrics` every 15s (configurable)
- Grafana connects to Prometheus
- Dashboard auto-loads on container startup

---

### Task 7: Integration & End-to-End Testing

**Files:**
- Modify: `tests/test_endpoint.py` — add e2e tests for caching + accounting
- Create: `tests/test_e2e_phase2.py` — full flow: rate limit → cache → cost log → metrics

**Test scenarios:**
1. Request A → cache miss → logged to DB
2. Request A again → cache hit → cost saved, not logged as new request
3. Request B (similar to A) → semantic match → cost saved
4. Rate limit exhaustion → 429 with Retry-After
5. Prompt update invalidates cache
6. Metrics endpoint returns valid Prometheus format

**Manual smoke test:**
```bash
docker compose up -d
curl -H "Authorization: Bearer $KEY" http://localhost:8100/v1/chat/completions \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'  # miss
curl ... same request ...  # hit (check response headers for cache marker?)
curl http://localhost:8100/metrics | grep gatekeep_cache_exact_hits
open http://localhost:3000 (Grafana)  # see cost dashboard
```

---

## Phase 2 Definition of Done

- `pytest -v` fully green including new Phase 2 tests
- Rate limit enforced: 100 tokens/min per key (or configured value)
- Exact cache: identical requests return cached response within 1s
- Semantic cache: similar requests (cosine sim > 0.95) return cached response
- Cost accounting: every request logged to `request_logs` with token counts and cost
- Prompt registry: `gatekeep prompt` CLI tools work end-to-end
- Prometheus `/metrics` endpoint returns valid Prometheus metrics
- Grafana dashboard accessible at `http://localhost:3000`, shows cost/usage/cache-hit-rate
- docker-compose includes Prometheus + Grafana services (auto-setup)
- Cache invalidation: prompt updates clear affected cached responses
- Documentation: README updated with rate limit + caching + cost examples

---

## Success Metrics

- **Cost visibility:** User can see per-request cost in dashboard
- **Spend control:** Rate limiting prevents accidental overspend
- **Cache effectiveness:** Typical production traffic sees 20-40% cache hit rate (depends on use case)
- **Operational ease:** Prompt updates don't break existing clients (version pointer handles it)

---

## Deferred to Phase 3

- Eval gate (blocks prompt promotion on regression)
- Automated curation pipeline (mine logs → eval dataset)
- GitHub Actions CI integration
- Cost-based routing (Haiku vs Sonnet decision)
