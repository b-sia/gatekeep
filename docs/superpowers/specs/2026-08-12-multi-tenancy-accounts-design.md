# Multi-tenancy via accounts - design

Status: **design complete - all decisions ratified.** Deployment shape chosen
(internal teams). Ready to turn into an implementation plan.

## Problem

Two related gaps in the current single-layer API-key model:

1. **No data isolation.** `require_api_key` (`gatekeep/middleware/auth.py:35`)
   authenticates a key but does not scope anything to it. The dashboard
   endpoints take `key_id` as a *client-supplied* query filter
   (`gatekeep/api/dashboard.py:175`, `:294`, `:400`), so any valid key can read
   any other key's usage, cost, and latency data by passing a different
   `key_id` - or omit it and read the cross-key aggregate.
2. **Keys are not distinguishable.** `ApiKey.name` has no unique constraint
   (`gatekeep/models.py:35`), so several keys can share a display label. The
   dashboard's per-key breakdown surfaces that label
   (`gatekeep/api/dashboard.py:159-160`), making it ambiguous which key owns
   which data.

## Chosen direction

Introduce **accounts (tenants) as the structural layer; API keys become an
access method onto that layer**, not the identity itself.

- New `accounts` table.
- `ApiKey.account_id` FK -> `accounts.id`.
- Every data query is scoped by `account_id` **derived server-side from the
  authenticated key**. `account_id` is never accepted as a client-supplied
  parameter.
- Keys become rotatable/disposable credentials. Rotating or revoking a key no
  longer orphans its history, because history hangs off the account.

Rejected alternative: scoping everything directly by `key_id`. It cannot
support multiple keys per tenant, per-tenant budgets, or key rotation without
losing data continuity.

### Deployment shape: internal teams (with an external-ready schema)

Decision (Q7): target **internal teams, each holding a key**. Tenants are
trusted colleagues, so isolation is about clean attribution and preventing
accidental cross-reads, not defending against hostile probing. This keeps
admin and self-serve tooling light (see decisions 5 and 6).

The schema does not bet on this choice. The alternative shape (external
orgs/customers) wants the *same* core tables; it differs only in enforcement
strictness and tooling depth, neither of which changes today's schema. So the
build stays safe even if the deployment shape later flips to external.

## Decisions made

### 1. Response cache: partition per account

Today the cache is fully global. `find_semantic_match`
(`gatekeep/middleware/cache_semantic.py:98`) filters only on `model` and
`prompt_version_num` - there is no key or tenant filter - and
`CachedResponse.exact_hash` is globally unique (`gatekeep/models.py:202`). One
caller's completion can therefore be served verbatim to another.

Decision: **add `account_id` to `cached_responses`**; make `exact_hash` unique
per `(account_id, exact_hash)`; filter semantic matches by `account_id`.

Trade-off accepted: lower cache hit rate and higher spend (two accounts asking
the same question each pay once) in exchange for no cross-tenant content
leakage and clean cost attribution.

### 2. Prompts: stay global and operator-managed

Prompts are **not tenant data**. Each `prompts/*.txt` is the version-controlled
source of truth, changes go through PR review plus a CI eval gate, and
`gatekeep prompt promote` refuses to activate a version scoring below its suite
threshold (`prompts/README.md`). There are **no write endpoints for prompts** -
verified, `gatekeep/api/` exposes no create/update routes. Authoring is CLI +
git only.

Per-account prompts would require tenants to have commit access to the
operator's repo, or the operator to run per-account directories and CLI
invocations on their behalf. That does not fit the governance model.

Decision: **`prompts` and `prompt_versions` stay global.** No `account_id`.

### 3. Eval gate: shared gate, account-tagged cases

The real tenant-data exposure is one layer below the templates. `curate_cases`
(`gatekeep/curation.py:45`) mines `recent_samples(...)` -> `request_samples`,
which stores verbatim tenant request messages and model output
(`gatekeep/models.py:222`, fields `input_messages` / `output_text`). Those
become `eval_cases` on a suite whose `prompt_name` is globally unique
(`gatekeep/models.py:250`). So one account's real traffic becomes a permanent
part of the gate governing every other account's promotions.

Mitigating factor already in place: curated cases land `reviewed=False` and a
human approves each via `gatekeep eval review` before it counts, so the
crossing is not silent.

Arguments for keeping ONE shared gate:
- Promotion to the active/production slot is a single global decision.
  `Prompt.active_version_id` is one column - one production version for
  everyone. (A `candidate_version_id` + `candidate_traffic_pct` canary can run
  alongside it, but that is a transient traffic split, not a second active
  version, and it does not run the gate - `gatekeep/models.py:118-128`.) A
  per-account gate yields N verdicts for a 1-bit decision, forcing invented
  semantics (promote if all pass? 80%?).
- Suite strength scales with traffic volume; splitting traffic N ways yields N
  thin, weak suites. A low-volume account cannot build a meaningful gate.
- Cost: each eval run is LLM calls (generation + a judge call per case). One
  suite = one run per promotion; per-account = N runs x cases.
- One interactive review queue instead of N.

Arguments against:
- Tenant content persists in a shared catalog after approval.
- **Tyranny of the majority**: curation samples recent traffic, so a dominant
  account's patterns define the suite. A change that is great for them and bad
  for a small account passes cleanly; the small account gets no signal and no
  veto. This is a quality failure mode, not only a privacy one. (Materially
  mitigated at runtime by the prompt-ops direction below.)
- Blast radius: one active version + one gate means a missed regression hits
  every account at once. (Also mitigated by auto-rollback below.)

**Decision (Q1 - ratified):** gate *semantics* and case *provenance* are separable.
Keep exactly one shared gate (preserving all four benefits) while tagging every
eval case with the account its sample came from. That buys attribution, audit,
filtering, and per-tenant deletion, changes no gating behavior, and preserves
the option to split into per-account gates later. Retrofitting `account_id`
onto a pile of mixed-provenance cases later is materially harder than adding
the column now.

**Why a shared gate is safe here - the runtime veto.** The two
arguments-against above (tyranny of the majority, blast radius) are both
*pre-promotion* concerns: a shared gate gives a small account no say before a
version goes active. A global "full prompt-ops" loop (article's Level 4) built
on the canary machinery that already exists (`candidate_version_id` +
`candidate_traffic_pct`) closes that gap at runtime rather than by sharding the
gate. The account layer this spec adds - `account_id` on `request_logs`, joined
to the `prompt_version_num` those logs already carry (`gatekeep/models.py:72`) -
yields per-account, per-version metrics for free. Monitoring keyed on those
metrics can trip **auto-rollback** (flip back to `previous_version_id`) when a
canary regresses *for any single account*, before it ever reaches 100%. So the
small account gets a runtime veto it never had at the pre-promotion gate. This
is a separate spec (prompt-ops loop, built on top of accounts), but it is the
reason a shared gate is the right call rather than a compromise: the per-tenant
signal that would otherwise motivate per-account gates is delivered by
monitoring, without splitting traffic into N thin, weak suites.

### 4. `request_samples`: add `account_id` (denormalized column)

`request_samples` holds verbatim tenant content (`input_messages` /
`output_text`, `gatekeep/models.py:222`) and already carries `key_id` (`:236`).
Decision (Q2): **add an `account_id` column**, written at capture time, rather
than joining through `key_id`. A denormalized column keeps provenance filtering
and per-tenant deletion working even after the originating key is rotated or
revoked. This is the substrate the eval-case provenance tags in decision 3 are
derived from.

### 5. Budget and rate limit: account-level pool

Today `monthly_budget_usd` is per-key (`gatekeep/models.py:44`, enforced in
`gatekeep/middleware/budget.py`) and rate limiting is per-key
(`gatekeep/middleware/ratelimit.py`). Decision (Q3): **move both to the
account** - the account (team) owns one shared quota; the per-key budget is
dropped. With one-account-per-key migration (decision 8), account-level limits
reproduce today's behavior exactly. Per-key sub-limits are deferred (YAGNI); the
schema can add a nullable per-key override later without rework.

### 6. Operator visibility: one boolean flag

Decision (Q4): add a single **`is_operator`** flag at the account level - no role
hierarchy, no RBAC. Regular keys see only their own account's data. The existing
cross-account dashboard breakdown (`gatekeep/api/dashboard.py:132-166`)
**survives but gates behind `is_operator`**, giving operators the fleet-wide
cost/latency view for capacity and spend oversight. Consistent with the
internal-teams shape ("Deployment shape" above).

### 7. `ApiKey.name` uniqueness: per account

Decision (Q6): `ApiKey.name` becomes **unique per `(account_id, name)`**, not
globally. Global uniqueness would leak one tenant's naming into another's
namespace; per-account scope fixes the original ambiguity complaint (Problem 2)
without that coupling.

### 8. Migration and backfill

Decision (Q5): at migration, **create one `account` per existing `ApiKey`**,
preserving today's behavior exactly (each key's current budget becomes its new
account's pool). `account_id` is **nullable during rollout**, backfilled, then
**tightened to NOT NULL** once every row carries one. Account **merging** is a
later, separate operation, out of scope for this migration.

### 9. `request_logs` scoping: direct column

`request_logs` is scanned on every dashboard aggregate. Decision: add a **direct
`account_id` column** rather than joining through `key_id` on each query -
consistent with `request_samples` (decision 4), and it keeps history
attributable after a key is revoked.

## Tables affected (working list)

| Table | Change |
|---|---|
| `accounts` | new; holds `monthly_budget_usd` + rate-limit config (decision 5) and `is_operator` (decision 6) |
| `api_keys` | + `account_id` FK; `name` unique per `(account_id, name)` (decision 7); per-key `monthly_budget_usd` removed (decision 5) |
| `request_logs` | + `account_id` direct column (decision 9) |
| `request_samples` | + `account_id` direct column (decision 4) |
| `cached_responses` | + `account_id`; `exact_hash` unique per `(account_id, exact_hash)` (decision 1) |
| `prompts` / `prompt_versions` | unchanged, global (decision 2) |
| `eval_suites` | unchanged, global (decision 3) |
| `eval_cases` | + source `account_id` tag (decision 3) |
| `eval_runs` | unchanged, global (decision 3) |
