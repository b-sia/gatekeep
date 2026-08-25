# Prompt Operations Dashboard - Design

**Date:** 2026-08-23
**Status:** Approved (design), pending implementation plan
**Author:** operator (brainstormed with Claude)

## Summary

Prompt lifecycle operations (create, version, promote, rollback, A/B
candidate configuration, eval-suite management, and curation review) are
currently only available through the `gatekeep` CLI. This design adds a
dedicated **Prompts** tab to the operator dashboard that surfaces the full
set of these operations through the UI, backed by new operator-gated
mutation endpoints, an in-process background-job channel for long-running
evals, and a general-purpose append-only **audit log** so every mutating
action leaves an actor-attributed trail.

The backend service functions already exist (`gatekeep/prompts.py`,
`gatekeep/evals.py`, `gatekeep/curation.py`); this work adds the HTTP
mutation layer, the audit + job infrastructure, and the React UI. Service
functions stay pure and unchanged.

## Goals

- Operators can perform every prompt lifecycle operation from the dashboard,
  without dropping to the CLI.
- Long-running eval operations (the eval gate on promote, and on-demand eval
  runs) never block the request or freeze the UI.
- Every mutating operation is auditable: who did what, to which entity, when,
  with what result (including *blocked* and *errored* attempts).
- The audit foundation is fleet-wide from day one, so account/key/operator
  events can be wired in later with no schema change.

## Non-Goals

- No task-queue / worker infrastructure (Celery/arq). In-process asyncio +
  Redis status is sufficient for the current single-instance deployment.
- No RBAC beyond the existing single `is_operator` boolean.
- Auditing of account/key/budget/operator-grant events is **designed for**
  (generic table) but not **wired** this iteration.
- Capturing security events (auth failures, budget denials, rate-limit
  blocks) is out of scope.
- No changes to request-time prompt resolution or the A/B split algorithm.

## Decisions (from brainstorming)

1. **Scope:** all four operation categories - version lifecycle, A/B candidate
   config, eval-suite management, curation review.
2. **Placement:** a new top-level **Prompts** tab (alongside Analytics and
   Accounts & Keys), master-detail layout. The existing read-only
   `PromptsPanel` / `EvalHistoryPanel` move here from Analytics.
3. **Long evals:** background job + UI polling.
4. **Job backbone:** Approach A - in-process `asyncio.create_task` +
   Redis-backed job status. No new services. A process restart loses an
   in-flight job (recoverable by re-running); completed results persist in
   `EvalRun` regardless.
5. **Audit:** a generic, fleet-wide `audit_event` table, built now with a
   read-only UI view; prompt + eval operations are the first producers.
   Account/key producers drop in later with no migration.

## Architecture

### Data model

**New table: `audit_event`** (append-only; new Alembic migration).

| column | type | purpose |
|---|---|---|
| `id` | PK int | |
| `created_at` | timestamptz, indexed | when the action happened |
| `actor_account_id` | FK accounts, nullable | which operator acted |
| `actor_label` | string | denormalized operator name at action time, so the log stays readable if the account is later renamed/deleted |
| `action` | string | namespaced verb, e.g. `prompt.promote`, `eval.run`, `curation.review` |
| `entity_type` | string | e.g. `prompt`, `eval_suite`, `curated_case` (later: `account`, `api_key`) |
| `entity_ref` | string, nullable | denormalized human id of the target (e.g. prompt name) |
| `version_num` | int, nullable | target version where relevant |
| `result` | string | `success` \| `blocked` \| `error` |
| `details` | JSON | action-specific: `{from_version, to_version, eval_score, passed, traffic_pct, case_count, error, ...}` |

Indexes: `(entity_type, entity_ref, created_at desc)`, `(created_at desc)`,
`(action, created_at desc)`.

`result = blocked` records a promotion stopped by the eval gate;
`result = error` records a failed operation (e.g. provider error during an
eval run). Both are first-class audit outcomes, not just successes.

**Extended reads:** `GET /prompts/{name}/versions` gains each version's
template text (needed to view/diff a version before promoting).

No other schema changes. `EvalRun`, `EvalSuite`, `EvalCase`, `RequestSample`,
`Prompt`, `PromptVersion` are unchanged.

### Actor attribution

Dashboard callers authenticate as an `Account` via `require_operator`. For
every mutation: `actor_account_id = caller.id`, `actor_label = caller.name`,
and prompt/version `created_by` is set to the operator's account name (today
only the CLI passes `created_by`).

### Audit writes live in the API layer

Audit events are written by the endpoint layer (a small `gatekeep/audit.py`
helper), **not** inside `prompts.py` / `evals.py`. Each endpoint calls the
existing pure service function, then records the audit event with its
outcome. The two async endpoints (promote, eval-run) write their audit event
from the background task on completion, so `blocked` / `error` outcomes are
captured accurately. This keeps the service layer pure and unchanged.

### Background-job channel (Redis, Approach A)

Long-running operations return a job id immediately and run in an
`asyncio.create_task`.

- Redis key `promptjob:{uuid}` holds JSON:
  `{id, kind: eval_run|promote, prompt_name, version_num,
    status: queued|running|succeeded|failed|blocked,
    progress: {done, total}, result: {score, passed}, error,
    created_at, updated_at}`, with a TTL after completion (e.g. 1 hour).
- The task transitions `queued -> running`, updates `progress` per eval case,
  and on completion writes the `EvalRun` row (via existing
  `run_suite_for_prompt`), sets the terminal status + `result`, and writes the
  audit event.
- For **promote**: the job runs the eval gate (if a suite exists) then the
  version flip inside `promote_prompt`. A gate failure yields terminal status
  `blocked` (audit `result = blocked`); success yields `succeeded`. Promotes
  of prompts with no suite complete near-instantly through the same path.
- The UI polls `GET /prompts/jobs/{job_id}`.

Sync vs async split:
- **Async (job + poll):** `eval-run`, `promote`.
- **Synchronous (fast DB ops):** create prompt, add version, rollback,
  set/clear candidate, create suite, add case, curation mine, curation review.

### API surface

All under the existing dashboard router, all `require_operator`-gated.

**Reads** (⟳ exists, extend):
- ⟳ `GET /prompts` - list + active version.
- ⟳ `GET /prompts/{name}/versions` - **extend** to include each version's
  template text.
- `GET /prompts/{name}/suite` - eval suite + its cases.
- `GET /prompts/{name}/curation` - unreviewed curated cases.
- ⟳ `GET /evals` - eval run history (filterable by prompt).
- `GET /audit?entity_type=&entity_ref=&action=&limit=` - paginated audit feed.
- `GET /prompts/jobs/{job_id}` - poll job status.

**Writes** (new):
- `POST /prompts` - create `{name, template, notes}`.
- `POST /prompts/{name}/versions` - add version `{template, notes}`.
- `POST /prompts/{name}/promote` `{version_num}` → returns job id (async, gated).
- `POST /prompts/{name}/rollback` - sync.
- `PUT /prompts/{name}/candidate` `{version_num, traffic_pct}` - set/adjust.
- `DELETE /prompts/{name}/candidate` - clear.
- `POST /prompts/{name}/suite` - create suite `{threshold?}`.
- `POST /prompts/{name}/suite/cases` - add a reviewed case.
- `POST /prompts/{name}/eval-run` `{version_num, model?}` → returns job id (async).
- `POST /prompts/{name}/curation/mine` - mine recent samples → unreviewed cases.
- `POST /prompts/{name}/curation/{case_id}/review` `{approved}`.

### Frontend (Prompts tab)

- New `TabKey` `"prompts"` in `Header`. `PromptsPage` gated on
  `me.is_operator`; non-operators see a "requires operator" notice, matching
  the accounts pattern.
- **Master-detail layout:**
  - **Left:** prompt list (name, active version, candidate badge) + "New
    prompt" button.
  - **Right - selected prompt detail**, stacked sub-sections:
    1. **Versions** - timeline table (moved `PromptsPanel`) with template
       text; "Add version" (textarea editor); per-version **Promote** /
       **Rollback** behind a confirm modal (promote warns it runs the eval
       gate + invalidates cache).
    2. **A/B candidate** - current candidate + traffic %, set/adjust, clear.
    3. **Evals** - suite + cases, "Run eval" (→ job), and eval history
       (`EvalHistoryPanel` scoped to the prompt).
    4. **Curation** - "Mine samples" + approve/reject list.
  - **Audit** - read-only feed, filterable to the selected prompt, plus a
    global view.
- **Job UX:** promote / eval-run kick off a job, then a `useJob(jobId)` hook
  polls and renders queued → running (progress `done/total`) →
  succeeded/blocked/failed inline; on success the relevant panel refetches.
- New client fns in `dashboard/src/api/client.ts` + types in `types.ts`,
  mirroring the existing account/key mutation pattern (`createAccount`,
  `revokeKey`).

### Error handling

- Mutations reuse the OpenAI-shaped error envelope + `require_operator` 403s.
- Service errors map to HTTP: `PromptNotFoundError` /
  `PromptVersionNotFoundError` → 404; duplicate-name and invalid
  `traffic_pct` `ValueError` → 400; `EvalGateFailure` surfaces as a job
  `blocked` result (not a 500), rendered as "promotion blocked by eval gate,
  score X".
- Frontend reuses `useApiErrorHandler` / `UnauthorizedError` (401 → identity
  picker); per-panel inline errors otherwise.
- The job poller handles a missing/expired job id (TTL lapsed) gracefully →
  "status unavailable, refresh."

## Testing

- **Service/API (pytest):** each new endpoint - happy path + mapped error
  cases; assert exactly one `audit_event` per mutating call with the correct
  actor / action / entity_ref / result (including a `blocked` promote and an
  `error` run); async endpoints return a job id and the background task drives
  Redis status + writes `EvalRun` + audit on completion. Reuse the existing
  test-DB and operator-auth fixtures.
- **Frontend (vitest):** new `client.ts` fns and the `useJob` polling hook
  (queued → running → terminal, and the expired-job path), mirroring
  `client.test.ts` / `identityStore.test.ts`.

## Risks & trade-offs

- **In-flight jobs lost on restart (Approach A):** an eval running when the
  process restarts is lost; the operator re-runs it. Completed results persist
  in `EvalRun`, so no data is lost - only the transient "in progress" job.
  Acceptable for a single-instance operator tool; revisit if the deployment
  goes multi-replica (would motivate Approach C, a real queue).
- **Concurrent promotes of the same prompt:** the DB transaction in
  `promote_prompt` keeps the flip atomic; a duplicate-in-flight-job guard is
  deliberately omitted for now (YAGNI) and can be added to the job channel
  later.
- **Audit completeness:** only prompt/eval producers are wired this iteration;
  account/key/operator/budget mutations remain unaudited until their producers
  are added (no schema change needed).
