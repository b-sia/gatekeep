# gatekeep Phase 3 — Eval Gate & Prompt Quality Control

**Goal:** Prevent prompt regressions from reaching production by gating promotion behind automated evals, and use production traffic to keep those evals representative over time.

**Key deliverables:**
- Eval case schema + runner (deterministic checks + LLM-judge checks)
- `gatekeep prompt promote` blocked unless the target version passes its eval suite
- Curation pipeline: mine `request_logs` into candidate eval cases
- GitHub Actions workflow that runs evals on prompt-template PRs
- Cost-based routing: pick cheapest model that clears a quality bar

**Tech additions:** none required beyond what Phase 2 already added (Anthropic client, Postgres, Redis); an LLM-judge check reuses the existing provider client rather than adding a new dependency.

---

## Rationale

Phase 2 gave prompts version history and a promote/rollback workflow, but promotion is currently a bare pointer flip (`gatekeep/prompts.py:107` `promote_prompt`) — nothing stops a bad template from going active. Phase 3 closes that gap:

1. **Eval gate** - a prompt version can't become active until it passes a scored test suite
2. **Curation pipeline** - eval cases are grown from real traffic (`request_logs`) instead of hand-written only, so the suite tracks what users actually send
3. **CI integration** - the same gate runs in GitHub Actions so a bad prompt is caught in review, not after promotion
4. **Cost-based routing** - once quality is measured, route to the cheapest model that still clears the bar, closing the loop between Phase 2's cost accounting and Phase 3's quality signal

---

## Architecture Changes

```
gatekeep prompt promote <name> <version>
        │
        ▼
  [load eval suite for <name>]  ──►  no suite registered? allow promote (opt-in gate)
        │
        ▼
  [eval runner: render template against each case's input]
        │
        ▼
  [score: exact/rule checks + LLM-judge checks]  ──►  Anthropic client (existing provider)
        │
        ▼
  score >= suite.pass_threshold?
        │
   yes ──┴── no
   │          │
   ▼          ▼
 promote   raise EvalGateFailure (block promotion, print report)
```

```
request_samples (new; written on cache-miss path)
        │
        ▼
[curation CLI: sample recent samples for a prompt_name]
        │
        ▼
[write candidate EvalCase rows, unreviewed]
        │
        ▼
human review (CLI: approve/reject) ──► reviewed EvalCase feeds eval runner
```

New Postgres tables:
- `request_samples` — key_id (fk `api_keys`), prompt_name (nullable), model, input_messages (jsonb, structured), output_text, created_at (see decided Question 4)
- `eval_suites` — name, prompt_name (fk-by-name to `prompts.name`), pass_threshold, created_at
- `eval_cases` — suite_id, input (jsonb messages), expected (jsonb, nullable for judge-only cases), check_type (`exact` | `contains` | `llm_judge`), reviewed (bool), source (`manual` | `curated`), created_at
- `eval_runs` — suite_id, prompt_version_id, model, score, passed (bool), report (jsonb: per-case results), created_at

Also add `prompt_name` (nullable) to the existing `request_logs` table — it is request-level metadata whose absence there is a pre-existing gap (see decided Question 4).

No new Redis structures. No new external services — the LLM-judge check calls the same provider client Phase 1/2 already wired up.

---

## Phase 3 Tasks

### Task 1: Eval Schema & Case Storage

**Files:**
- Modify: `gatekeep/models.py` — add `EvalSuite`, `EvalCase`, `EvalRun` tables
- Create: `migrations/versions/<rev>_add_eval_tables.py` (via `alembic revision --autogenerate`)
- Test: `tests/test_eval_models.py`

**Schema:**
```python
class EvalSuite(Base):
    __tablename__ = "eval_suites"

    id: int (pk)
    prompt_name: str  # matches Prompt.name; no FK since prompts can be deleted independently
    pass_threshold: float  # 0.0-1.0, fraction of cases that must pass
    created_at: datetime


class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: int (pk)
    suite_id: int (fk EvalSuite)
    input_messages: JSON  # list[dict] - the user/system messages to render the prompt against
    expected: str (nullable)  # required for check_type in (exact, contains); unused for llm_judge
    check_type: str  # "exact" | "contains" | "llm_judge"
    judge_criteria: str (nullable)  # required when check_type == "llm_judge"
    reviewed: bool  # false for curated-but-unreviewed cases
    source: str  # "manual" | "curated"
    created_at: datetime


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: int (pk)
    suite_id: int (fk EvalSuite)
    prompt_version_id: int (fk PromptVersion)
    score: float  # fraction of cases passed
    passed: bool  # score >= suite.pass_threshold
    report: JSON  # [{case_id, passed, actual_output, reason}, ...]
    created_at: datetime
```

**Behavior:**
- `EvalCase.reviewed` defaults `False` for curated cases so the runner can optionally exclude unreviewed cases (`--include-unreviewed` flag on the CLI)
- `check_type == "exact"` compares rendered output to `expected` verbatim; `"contains"` checks substring; `"llm_judge"` sends output + `judge_criteria` to the provider and parses a pass/fail verdict

---

### Task 2: Eval Runner

**Files:**
- Create: `gatekeep/evals.py` — suite loading, case execution, scoring
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `gatekeep.prompts.get_active_prompt_version` (or an explicit `PromptVersion` for evaluating a not-yet-promoted draft), `gatekeep.providers` client (existing Anthropic wrapper)
- Produces:
  - `async def run_eval_suite(suite_name: str, prompt_version: PromptVersion, session: AsyncSession) -> EvalRun`
  - `class EvalGateFailure(Exception)` — raised by the promote path when `score < pass_threshold`

**Behavior:**
- For each `EvalCase`, render `prompt_version.template` against `input_messages`, call the provider, score per `check_type`
- `llm_judge` cases send a fixed judge prompt: `"Given criteria: {judge_criteria}\n\nOutput: {actual}\n\nDoes the output satisfy the criteria? Answer PASS or FAIL and one sentence why."` — parse the leading PASS/FAIL token
- Persist one `EvalRun` row with the full per-case `report`, so a failed gate has a paper trail without re-running

---

### Task 3: Wire the Gate into Promotion

**Files:**
- Modify: `gatekeep/prompts.py:107` `promote_prompt` — check for a registered `EvalSuite` before flipping the pointer
- Modify: `gatekeep/cli.py` — surface `EvalGateFailure` as a non-zero exit with the report printed
- Test: `tests/test_prompts.py` — add regression case: promotion blocked when eval fails, allowed when it passes, allowed when no suite is registered (opt-in gate, matches Phase 2 prompts that never get a suite)

**Behavior:**
- If no `EvalSuite` row exists for `prompt_name`, `promote_prompt` behaves exactly as it does today (ungated) — this keeps the gate additive, not a breaking change for Phase 2 prompts
- If a suite exists, `promote_prompt` calls `run_eval_suite` first; on failure it raises `EvalGateFailure` and does **not** touch `active_version_id` or invalidate any cache
- `rollback_prompt` (`gatekeep/prompts.py:159`) is **not** gated — reverting to a previously-active (already-proven) version should never be blocked by an eval regression

---

### Task 4: Curation CLI

**Files:**
- Create: `gatekeep/curation.py` — sampling logic over `request_logs`
- Modify: `gatekeep/cli.py` — add `gatekeep eval curate <prompt_name> --limit N`, `gatekeep eval review <case_id> --approve/--reject`
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes: `RequestSample` rows filtered by `prompt_name` (see decided Question 4 — curation mines a dedicated `request_samples` corpus, **not** `request_logs`, which stores no message content, and **not** `cached_responses`, whose rows are deduped and deleted on every promote)
- Produces: unreviewed `EvalCase` rows with `source="curated"`, `check_type="llm_judge"` and a generic `judge_criteria` ("output is a coherent, on-topic response to the input") as a starting point for human tightening

**Behavior:**
- Sample the most recent N `request_samples` for a given `prompt_name` (the corpus only contains cache-miss fresh traffic by construction, since it is written on the cache-miss path — cache hits, which don't reflect new template behavior, never enter it)
- `gatekeep eval review` lists unreviewed cases one at a time and flips `reviewed=True` on approve, deletes on reject — this is the human-in-the-loop step; nothing curated becomes gate-enforcing until reviewed

---

### Task 5: GitHub Actions CI Integration

**Files:**
- Create: `.github/workflows/eval-gate.yml`
- Create: `scripts/ci-eval-check.sh` — thin wrapper that calls the eval runner against a template file changed in the PR diff

**Behavior:**
- Trigger on PRs touching prompt template files (path filter, e.g. `prompts/**`) - per the decided Open Question 1, templates live in-repo as files, with the DB row populated from the file on create/merge
- Job spins up the same Postgres+Redis services as `tests/` (reuse `docker-compose.yml` service definitions or a CI-only compose override), runs `alembic upgrade head`, then `scripts/ci-eval-check.sh <prompt_name> <template_file>`
- Script creates a throwaway `PromptVersion` (not promoted), runs `run_eval_suite` against it, exits non-zero on failure so the PR check goes red

---

### Task 6: Cost-Based Routing

**Files:**
- Create: `gatekeep/routing.py` — model selection logic
- Modify: `gatekeep/app.py` — optional routing step before the provider call
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: `gatekeep.accounting.MODEL_PRICING` (existing), `EvalRun.score` history per model
- Produces: `def select_model(requested_model: str, quality_floor: float, session: AsyncSession) -> str`

**Behavior:**
- Opt-in per request via a `route_by_cost: true` field (or per-key config) - Phase 3 should not silently override a client's explicit `model` choice by default
- When enabled: look at the most recent `EvalRun` for whatever suite applies to the request's `prompt_name` across candidate models cheaper than `requested_model`; if a cheaper model has a passing run at or above `quality_floor`, substitute it and log the substitution in `RequestLog` (needs a `routed_from` column, or reuse `cache_key`-style metadata field)
- This task is the most speculative of the six and depends on evals being scoped per-model, not just per-prompt-version - flag for a design check before implementation rather than building straight from this description

---

## Phase 3 Definition of Done

- `pytest -v` fully green including new eval tests
- `gatekeep prompt promote` blocks on a failing eval suite and prints a report; unblocked promotion for prompts with no suite registered
- `gatekeep eval curate` pulls real traffic into unreviewed `EvalCase` rows; `gatekeep eval review` lets a human approve/reject
- GitHub Actions workflow runs on prompt-template PRs and fails the check on eval regression
- Cost-based routing is implemented behind an opt-in flag and never silently overrides an explicit model request
- Documentation: README updated with eval gate + curation workflow examples

---

## Decided Questions

Decisions below are final. All options are kept in place (rather than deleted) so the rejected alternatives and their tradeoffs remain available for backtracing.

### 1. Prompt template storage: in-repo files vs DB-only

Decides whether Task 5's CI trigger is a path filter on repo files, or something else entirely.

**Option A - DB-only (current Phase 2 behavior).** `gatekeep prompt create <name> <template_file>` reads the file once; all history after that lives in `prompt_versions`. The file itself is irrelevant post-creation.
- Pros: no migration needed, already how Phase 2 works; promotion/rollback stays a DB pointer flip decoupled from deploys; non-engineers could manage prompts via CLI/API without touching a repo.
- Cons: a prompt change isn't a PR - nothing to diff, review, or comment on in GitHub; Task 5's "CI runs on prompt-template PRs" has no natural trigger since no repo file changes when a prompt is edited; gating in CI would need an awkward out-of-band "propose" step.

**Option B - in-repo files** (e.g. `prompts/system-context.txt`), DB row populated *from* the file on create/merge.
- Pros: natural PR review (template changes show up as a diff, get comments/approval like code); Task 5's trigger becomes trivial (`on: pull_request, paths: ["prompts/**"]`); git history becomes the audit trail.
- Cons: two sources of truth to keep in sync (file vs DB row) unless enforced via a merge-triggered sync; less flexible for a future admin UI where prompts are promoted without going through git.

**Decision: Option B (in-repo files).** Phase 3's whole point is gating changes before they ship, and a PR is the natural place for both human review and the automated gate to happen together.

### 2. LLM-judge model: self-judging vs fixed stronger judge

**Option A - self-judging.** The model under test also grades its own output against `judge_criteria`.
- Pros: cheap, fast, no extra model call.
- Cons: correlated blind spots - if a prompt change causes a specific misunderstanding, the same model grading its own output tends to share that misunderstanding, so a genuinely broken output can pass. This is a documented self-preference bias in LLM-as-judge setups. Tolerable for a rough smoke-test signal, not for the thing standing between a bad prompt and production.

**Option B - fixed stronger judge** (e.g. always Sonnet, independent of which model/version is under test).
- Pros: independent perspective, doesn't share the generator's specific failure modes; consistent judge means `EvalRun.score` stays comparable across runs over time; matches standard LLM-as-judge practice (judge deliberately decoupled from generator).
- Cons: extra cost per eval run (minor - runs at promotion/CI time, not per production request, so volume is low); judge can still be wrong/inconsistent, this mitigates but doesn't guarantee correctness; slightly more moving parts in `gatekeep/evals.py` (must call a specific fixed model rather than whatever the request's provider client is).

**Decision: Option B (fixed stronger judge).** Self-judging risks the gate rubber-stamping the exact failure mode it exists to catch.

### 3. `pass_threshold`: per-suite vs gateway-wide default

**Option A - single gateway-wide default** (e.g. `Settings.eval_pass_threshold = 0.9`), applied to every `EvalSuite`.
- Pros: simple, no per-suite decision required when creating a new prompt; consistent bar across the whole gateway.
- Cons: prompts have different risk profiles (customer-facing vs internal-debug-tool) that one threshold either over-blocks or under-protects; suites with a different check-type mix behave differently under judge noise - a suite of mostly `exact`/`contains` checks can reasonably demand 100%, one that's mostly `llm_judge` shouldn't, or it'll see flaky failures from judge variance rather than real regressions.

**Option B - per-suite** (`EvalSuite.pass_threshold`, as already reflected in the Task 1 schema above), optionally defaulted from a gateway-wide setting at creation time.
- Pros: matches the reality that not all prompts carry equal risk; a judge-heavy suite can use a looser threshold without loosening every other suite; collapses into Option A automatically if every suite just uses the same default and nobody overrides it.
- Cons: one more field to think about when setting up a new prompt (mitigated by defaulting it); marginally more to document.

**Decision: Option B (per-suite).** It's strictly more flexible and degenerates to Option A if unused.

### 4. Curation source: where does mined content live?

Surfaced during implementation planning: Task 4 as originally written said curation mines `request_logs`, but `request_logs` persists only metadata (tokens, cost, `response_id`) — no message content and no `prompt_name` — so an `EvalCase` (which needs `input_messages`) cannot be built from it. This forced a decision on where the curation corpus lives.

**Option A - mine `cached_responses`.** It already stores `user_messages_text`, `response_text`, `model`, and `prompt_name`.
- Pros: no new table; content already present for the cache-miss requests curation cares about.
- Cons: three lifecycle mismatches make it wrong as a corpus — (1) rows are **deleted on every promote/rollback** (`prompts.py:151` `delete_cached_responses_by_prompt`), so the source is wiped exactly for the prompts Phase 3 iterates on most; (2) rows are **deduped by `exact_hash`**, destroying the frequency signal that defines "representative"; (3) content is the flattened `user_messages_text`, not the structured message list.

**Option B - put content on `request_logs`.** Add `prompt_name` + `input_messages` + `output_text` to the accounting log.
- Pros: fewest tables; one record per request.
- Cons: overloads the lean accounting log with full content (PII surface + storage on a hot write path) and mixes accounting with curation concerns.

**Option C - dedicated `request_samples` table.** Append-only, written at the cache-miss call site (`app.py:257`), carrying structured `input_messages` + `output_text` + `model` + `prompt_name`; never deduped, never deleted on promote, own retention policy.
- Pros: decouples the eval corpus from both cache invalidation and accounting (correct single responsibility); structured input; only stores content for the cache-miss fresh-traffic subset curation actually uses.
- Cons: one more table and one more write on the cache-miss path (marginal — that path already does a DB write via `store_cached_response`).

**Decision: Option C (dedicated `request_samples` table).** The decisive factor is lifecycle: `cached_responses` is deleted on the very promotions Phase 3 gates, so it cannot serve as a durable corpus. Separately, `prompt_name` **is** request-level metadata whose absence on `request_logs` is a pre-existing gap, so it is also added to `request_logs` (independent of curation, for observability like "requests per prompt").

---

## Deferred beyond Phase 3

- Per-organization eval suites / multi-tenant isolation
- A/B testing prompt versions in production (partial traffic split) rather than binary promote/rollback
- Automated judge-criteria generation from curated cases (today it's a fixed generic criteria string)
