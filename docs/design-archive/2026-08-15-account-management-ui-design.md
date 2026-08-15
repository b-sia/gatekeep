# Account Management UI - Design

**Date:** 2026-08-15
**Status:** Approved (design), pending implementation plan
**Depends on:** the multi-tenancy accounts layer (PR #22, merged; migrations 0014-0020)

## Problem

Multi-tenancy is fully implemented at the data and API layer but has no
management surface. Today:

- Accounts exist only because migration 0014 backfilled one per existing API
  key. Every backfilled account has `is_operator = false`.
- The only account write path is `gatekeep key set-budget <name>` (sets an
  account's `monthly_budget_usd`).
- There is no way to create an account, mint a key for an account, revoke a
  key, or grant operator status except raw SQL. `scripts/create_key.py` is
  broken: it inserts an `ApiKey` with no `account_id`, which has been
  non-nullable since migration 0014.
- The `dashboard/` React app is a single analytics page with no account
  awareness - it does not even know the caller's account name or whether they
  are an operator.

This design adds a management UI (and the API + CLI + service layer beneath
it) for accounts and API keys.

## Scope and non-goals

**In scope:** self-service key management for every account; operator-level
management of all accounts and their keys; a CLI at parity with the API; a
shared service layer; the frontend tab and screens.

**Non-goals (deliberately excluded):**

- No account deletion (would orphan keys and request history).
- No key reactivation and no hard key deletion (soft revoke only).
- No RBAC or role hierarchy - `is_operator` stays a single boolean.
- No "last used" / per-key request counters (not tracked today; would be new
  columns and write-path work).
- No email/Slack notifications (out of scope, as with the existing budget
  alerts).
- No change to how prompts or the eval gate are scoped (they stay global).

## Authorization model

Two tiers, enforced server-side:

- **Any account (self-service):** manage its own keys (list / mint / revoke);
  view its own budget cap and month-to-date spend. Cannot change its own
  budget.
- **Operator (`is_operator = true`):** everything a tenant can do, plus manage
  all accounts (create / rename / set-budget / toggle-operator) and mint/revoke
  keys for any account.

The authz rule mirrors the existing `_account_scope` pattern in
`gatekeep/api/dashboard.py`: **an operator may target any `account_id`; a
non-operator may target only their own account, otherwise `403`.**

**Budget is a spend cap, not a self-service setting.** A tenant that could
raise its own cap would make the cap meaningless, so budget changes are
operator-only. Tenants see their cap and live spend read-only.

### Guardrails (enforced in the service layer, not just the UI)

- **Last-operator guard:** setting `is_operator = false` is rejected if it
  would leave zero operators (prevents locking everyone out of the operator UI).
- **Account name** is globally unique (`accounts_name_key`); collisions on
  create/rename return `409`.
- **Key name** is unique per account (`uq_api_keys_account_id_name`);
  collisions return `409`.
- **Budget** must be positive or explicitly cleared (null); non-positive values
  return `422`, matching the existing CLI validation.
- **Revoke** only ever affects keys belonging to the authorized account.

## Architecture

### Shared service layer (the core move)

A new module `gatekeep/account_service.py` holds all account/key business logic
as plain async functions. Both the CLI and the API call it, so there is one
code path to test and no logic duplicated between them.

Functions (names indicative):

- `create_account(session, *, name, monthly_budget_usd=None, is_operator=False) -> Account`
- `rename_account(session, account_id, new_name) -> Account`
- `set_budget(session, account_id, amount | None) -> Account`
- `set_operator(session, account_id, value: bool) -> Account`
  (raises on the last-operator guard)
- `list_accounts_with_stats(session, redis) -> list[...]`
  (each row: account fields + active/total key counts + month-to-date spend)
- `list_keys(session, account_id) -> list[ApiKey]`
- `create_key(session, account_id, name) -> tuple[ApiKey, str]`
  (returns the raw key; only its sha256 hash is persisted)
- `revoke_key(session, account_id, key_id) -> ApiKey`
  (sets `active = false`)

Reuses:

- `gatekeep.auth_keys.generate_key` / `hash_key` for minting.
- `gatekeep.middleware.budget.get_period_spend(session, redis, account_id=...)`
  for month-to-date spend. **Never `check_budget`** - that function fires the
  budget warning/exceeded alerts and increments the `budget_alerts_total`
  Prometheus counter, which must not happen on a dashboard read.

Uniqueness violations surface as SQLAlchemy `IntegrityError`; the service
layer translates them into a typed error the API maps to `409` and the CLI
prints as a message. The last-operator and non-positive-budget checks are
explicit in the service layer so both callers enforce them identically.

### Month-to-date spend: effects of reusing `get_period_spend`

Accepted and understood:

- It reads the Redis spend counter first; on a miss or Redis error it
  aggregates `request_logs` and **seeds Redis** with a `SET`. So a dashboard
  read can write to Redis. For an active account the counter is already warm,
  so the common case is a pure Redis `GET`. The pre-existing lost-update race
  between that seed and a concurrent `record_spend` `INCRBYFLOAT` already
  exists on the hot path; a dashboard caller only exercises it slightly more
  often. It self-heals each period and the overshoot is bounded - acceptable
  for a business control.
- The dashboard route gains a Redis handle dependency (it currently touches
  only Postgres). Redis is already a hard service dependency.
- The figure is **provider spend with cache hits excluded**
  (`_aggregate_spend_from_db` filters `cached.is_(False)`), matching what the
  budget cap enforces. It will legitimately read **lower** than the Analytics
  tab's cost figures (which include the notional cost of cache hits). The UI
  labels it as budget-relevant spend so the two tabs do not appear to disagree.

## API

All routes live under the existing `/dashboard/api/` prefix
(`gatekeep/api/dashboard.py` or a sibling router included the same way). A new
`require_operator` dependency builds on the existing `_require_caller_account`
and raises `403` when the caller is not an operator. "My keys" is expressed as
the account-scoped route for the caller's own id, so there is one key surface,
not two.

| Method & path | Access | Behavior |
|---|---|---|
| `GET /me` | any authenticated key | `{account_id, name, is_operator, monthly_budget_usd, spend_mtd}`. Drives tab visibility and the budget card. |
| `GET /accounts/{id}/keys` | own account, or operator | List that account's keys (`id, name, active, created_at`). |
| `POST /accounts/{id}/keys` | own account, or operator | Mint a key; response includes the raw key **exactly once**. |
| `POST /accounts/{id}/keys/{key_id}/revoke` | own account, or operator | Soft-revoke (`active = false`). |
| `GET /accounts` | operator only | All accounts with budget, month-to-date spend, and key counts. |
| `POST /accounts` | operator only | Create an account (`name`, optional `monthly_budget_usd`, optional `is_operator`). |
| `PATCH /accounts/{id}` | operator only | Rename, set/clear budget, and/or toggle operator (guarded). |

Error mapping: `403` (wrong tier / other account), `404` (unknown account or
key), `409` (name collision, last-operator guard), `422` (non-positive budget).
All error bodies follow the existing OpenAI-shaped error convention used by
`require_api_key`.

## CLI

The CLI must exist independently of the UI because it **bootstraps the first
operator** - with every account at `is_operator = false`, an operator-gated UI
is otherwise unreachable. New subcommand groups call the same service layer:

- `gatekeep account create <name> [--budget X | --unlimited] [--operator]`
- `gatekeep account rename <name> <new-name>`
- `gatekeep account set-budget <name> (<amount> | --unlimited)`
- `gatekeep account set-operator <name> [--off]`
- `gatekeep account list`
- `gatekeep key create <account> <name>` (prints the raw key once)
- `gatekeep key revoke <account> <name>`
- `gatekeep key list <account>`

`gatekeep key set-budget` moves to `gatekeep account set-budget` (budget is an
account-level concept). **Open question for review:** keep a back-compat alias
under `key set-budget`, or move it outright? Current lean: move it outright
(the feature is young; no external scripts depend on it yet).

`scripts/create_key.py` and `scripts/init-test-key.sh` are updated to go
through the service layer (creating an account when needed), fixing the current
breakage where they insert an `ApiKey` with no `account_id`.

## Frontend

The `dashboard/` app stays router-free. Changes:

- **Header** gains a tab control (`Analytics` | `Accounts & Keys`) backed by a
  `useState` toggle at the page root, matching the app's existing
  conditional-render style. The `Accounts & Keys` tab's operator section is
  shown only when `GET /me` reports `is_operator`.
- **New `pages/ManagementPage.tsx`** composing:
  - `BudgetCard` - cap + live month-to-date spend (view-only), from `GET /me`.
  - `KeyTable` - the caller's keys; create button and per-row revoke. Revoked
    keys remain listed, greyed out.
  - `CreateKeyModal` - two steps: (1) name the key; (2) show the raw key once
    with a copy button and a warning, gated by an **"I've saved it" checkbox**
    before the panel can be dismissed.
  - Operators additionally get `AccountsTable` (all accounts: name, budget,
    MTD spend, key count, operator flag, "Create account") and, via a
    `Manage ›` action, an `AccountDetailPanel` (rename / set budget / toggle
    operator / manage that account's keys) plus a `CreateAccountModal`.
- **`api/client.ts`** gains POST/PATCH helpers (it is GET-only today) with the
  same bearer-auth + 401-handling wrapper; **`api/types.ts`** gains the new
  response/request shapes.

### Navigation and screens (approved via mockups)

- Navigation: tabs in the header (not a sidebar, not one long page).
- Self-service view: budget card on top, keys table below.
- Operator view: an "All accounts" table with a row-level `Manage ›` opening a
  detail panel (not inline row editing).
- Mint flow: name -> show-once panel with copy, a can't-undo warning, and an
  "I've saved it" confirm before it closes.

## Testing

- **`tests/test_account_service.py`** (new): unit tests for the service layer -
  create/rename/set-budget/set-operator, the last-operator guard, key
  mint/revoke, `list_accounts_with_stats` including spend and key counts, and
  the uniqueness/validation errors.
- **`tests/test_dashboard.py`** (extended): route-level tests - `GET /me`
  shape; authz denials (a non-operator hitting operator routes; a non-operator
  targeting another account's keys); mint returns the raw key once; revoke
  flips `active`; name-collision and last-operator responses map to `409`.
- **CLI:** a test exercising at least the operator-bootstrap path
  (`account set-operator`) and `key create`, since these are the headless entry
  points.
- **Frontend:** verified manually - the `dashboard/` project has no test runner
  configured, so this matches the repo's current practice.

## Open questions for review

1. `key set-budget` -> `account set-budget`: move outright (current lean) or
   keep a deprecated alias?
2. Should `GET /accounts` (operator) also return each account's `created_at`
   and a total-vs-active key split, or just active counts? (Mockup shows
   "N active"; cheap to include both.)
