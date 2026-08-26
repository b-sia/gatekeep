# Self-Serve Signup with Operator Approval - Design

**Date:** 2026-08-26
**Status:** Approved (design), pending implementation plan
**Branch:** `feat/self-serve-signup`

## Problem

A user who visits a hosted Gatekeep instance cannot get started on their own.
The only way to obtain a GK API key today is out-of-band: an operator runs
`scripts/init-test-key.sh` / `scripts/create_key.py`, uses the
`gatekeep account`/`gatekeep key` CLI, or hits the operator-gated
`POST /accounts` + `POST /accounts/{id}/keys` routes. The dashboard's
`IdentityPicker` only accepts a key the user *already* holds. There is no
signup, no human login, and no self-service path to a first key.

## Goal

Give new users a conventional **email + password signup** that creates an
account in a `pending` state, lets an **operator approve** it (and set its
budget), after which the user logs in and manages (mints/revokes) their own
API keys using the self-service key management that already exists.

## Decisions (locked during brainstorming)

1. **Flow:** self-serve request, **operator approval**. Approved users then
   self-manage their own keys.
2. **Login:** email + password (conventional). New password storage, session,
   and password-reset infrastructure.
3. **Identity model:** a login user **is** an `Account`, **1:1**. No teams /
   multi-user-per-account. Operators remain accounts flagged `is_operator`.
4. **Email:** pluggable backend - `console` (dev/test) and generic `smtp`
   (production, stdlib, no vendor SDK). Provider choice is deployment config.
5. **Pending UX:** a pending user **can log in** and see a "pending approval"
   status page; key minting and API access stay blocked until approved.
6. **Dashboard auth:** management routes accept a **session cookie OR an API
   key** (route "A"). The paste-a-key `IdentityPicker` UI is **retired**.
7. **Sessions:** **server-side** sessions in the DB (opaque signed cookie
   referencing a `Session` row) for instant revocation.

## Non-goals (YAGNI)

- Teams / multiple users sharing one account / roles beyond the existing
  `is_operator` boolean.
- Operator "act as / impersonate another account" (the retired per-tab
  paste-key roster from PR #24 is not replaced; a future feature if needed).
- Vendor-specific email SDKs (SMTP covers SES/SendGrid/Mailgun/etc.).
- OAuth / social login / SSO.
- A `gatekeep account approve` CLI command (dashboard approval is the path;
  can be added later for headless ops).

## Existing code this builds on

- `gatekeep/storage/models.py:30` - `Account` (tenancy root; `monthly_budget_usd`,
  `is_operator`) and `ApiKey` (SHA-256 hashed, per-account, disposable).
- `gatekeep/accounts/account_service.py` - `create_account` (:95), `create_key`
  (:258), budget/stats helpers. Reused as-is.
- `gatekeep/accounts/auth_keys.py` - `generate`/`hash_key` (SHA-256) pattern,
  mirrored for session/email token hashing.
- `gatekeep/api/dashboard.py` - `_require_caller_account` (:66),
  `require_operator` (:170), `_authorize_account_access` (:192),
  `require_account_access` (:207); key routes `mint_account_key` (:1977) and
  `revoke_account_key` (:2010) already allow the account's own caller.
- `gatekeep/config.py` - `Settings` (pydantic-settings) + `get_settings()`.
- `gatekeep/app.py:215` - FastAPI app; mounts the React dashboard, includes
  `dashboard_router`. Pre-auth per-IP rate limiter already in `Settings`
  (`pre_auth_rate_limit_*`).
- `dashboard/src/App.tsx` - gates the UI on a pasted "active identity";
  `client.ts` sends `Authorization: Bearer <key>`; `IdentityPicker.tsx` /
  `identityStore.ts` implement the paste-key roster (to be retired).

## Data model

One new Alembic migration adds:

### `accounts.status` (new column)

`status`: `pending | approved | rejected | disabled`.
Server-default **`approved`** so every existing row and all
CLI/script/programmatic accounts remain fully functional and the operator
bootstrap path is untouched. Self-serve signups start `pending`.

### `account_credentials` (new table, 1:1 with `accounts`)

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `account_id` | int FK -> accounts.id, **unique** | 1:1 |
| `email` | str, **unique** | stored lowercased |
| `password_hash` | str | bcrypt (passlib) |
| `email_verified` | bool, default false | |
| `created_at` / `updated_at` | datetime | |

Separate table (not columns on `Account`) so CLI/programmatic accounts, which
have no human credentials, keep a clean tenancy row and the auth concern stays
isolated.

### `sessions` (new table)

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `token_hash` | str, unique | SHA-256 of the opaque cookie token |
| `account_id` | int FK -> accounts.id | |
| `created_at` | datetime | |
| `expires_at` | datetime | absolute expiry |
| `last_seen_at` | datetime | for idle tracking |

Only the hash is stored; the raw token lives solely in the cookie. Deleting a
row revokes the session instantly.

### `email_tokens` (new table)

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `purpose` | enum: `verify_email \| reset_password` | one table for both flows |
| `token_hash` | str, unique | SHA-256 of the raw token |
| `account_id` | int FK -> accounts.id | |
| `expires_at` | datetime | |
| `used_at` | datetime, nullable | single-use |

## Auth mechanics

- **Passwords:** bcrypt via **`passlib[bcrypt]`** (new dependency). SHA-256
  (keys) is intentionally not reused for passwords.
- **Session / email tokens:** `secrets.token_urlsafe`, stored as SHA-256 hash,
  matched by re-hashing the incoming value.
- **Unified identity resolution:** `_require_caller_account` is extended to
  resolve **session cookie first, then fall back to `require_api_key`**. Because
  `require_operator` and `require_account_access` build on it, they
  transparently accept either credential. The **gateway/proxy routes keep
  `require_api_key` only** and are untouched.
- **Approval gate:** a new `require_approved` dependency blocks key-minting and
  API actions for non-`approved` accounts even when authenticated. A pending
  session reaches only `/me`, account/password settings, and logout.
- **Session cookie:** `HttpOnly`, `Secure`, `SameSite=Lax`, same-origin (the
  dashboard is served by the same FastAPI app).
- **CSRF:** state-changing routes use a **double-submit CSRF token** - the
  server sets a non-HttpOnly `csrf_token` cookie on login; the SPA echoes it in
  an `X-CSRF-Token` header, which the server matches against the cookie.
  Combined with `SameSite=Lax` and same-origin, this defends cookie-authed
  mutations. API-key-authed callers (no cookie) are exempt.

## Email layer

New `gatekeep/email/` package:

- `EmailBackend` protocol: `send(to: str, subject: str, body: str) -> None`.
- `ConsoleEmailBackend` - logs the full message incl. link; dev/test default.
- `SmtpEmailBackend` - stdlib `smtplib` + `email.message`; production.
- `get_email_backend()` factory mirroring `get_settings()`.

New `Settings` fields: `email_backend: Literal["console","smtp"] = "console"`,
`email_from`, `smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`,
`smtp_use_tls`, and **`public_base_url`** (used to build verification/reset
links).

## API routes

New `auth` router (unauthenticated except where noted):

| method + path | purpose |
|---|---|
| `POST /auth/signup` | `{email, password}` -> create `Account(pending)` + credential(unverified), issue `verify_email` token, send email. `202`, no enumeration. |
| `POST /auth/verify-email` | `{token}` -> set `email_verified=true`; account enters the pending queue. |
| `POST /auth/login` | `{email, password}` -> verify, create `Session`, set cookies. Body returns account `status`. Refuses `rejected`/`disabled`. |
| `POST /auth/logout` | revoke the session row, clear cookies. |
| `POST /auth/password/reset-request` | `{email}` -> issue `reset_password` token + email. Always `202` (no enumeration). |
| `POST /auth/password/reset` | `{token, new_password}` -> set new hash, **revoke all sessions** for the account. |

Operator routes (gated by `require_operator`):

| method + path | purpose |
|---|---|
| `GET /accounts?status=pending` | list the pending-approval queue. |
| `POST /accounts/{id}/approve` | `{monthly_budget_usd}` -> status `approved` + budget; send approval email. |
| `POST /accounts/{id}/reject` | status `rejected`. |

Existing `GET /me`, `POST /accounts/{id}/keys`, and
`POST /accounts/{id}/keys/{key_id}/revoke` are reused unchanged for the
logged-in user's own key management.

**Abuse controls:** `signup`, `login`, `reset-request` sit behind the existing
pre-auth per-IP rate limiter.

## Frontend

- **New pages:** `LoginPage`, `SignupPage`, `VerifyEmailPage`,
  `ForgotPasswordPage`, `ResetPasswordPage`, `PendingApprovalPage`.
- **`App.tsx` gating** changes from "active pasted identity" to session: on
  mount, `GET /me` with `credentials: 'include'`. `401` -> Login/Signup;
  `status=pending` -> `PendingApprovalPage`; `approved` -> dashboard.
- **`client.ts`** switches from `Authorization: Bearer <key>` to cookie-based
  (`credentials: 'include'`) plus the `X-CSRF-Token` header on mutations.
- **Retire** `IdentityPicker.tsx` and `identityStore.ts` (and their tests);
  the per-tab paste-key roster from PR #24 is removed with them.
- **Operator UI:** a **Pending Requests** section (extending `AccountsTable` /
  the management page) listing `pending` accounts with **Approve** (budget
  input) / **Reject** actions.
- Key management (`CreateKeyModal`, `KeyTable`) is unchanged and now reachable
  by the logged-in account owner.

## Migrations / CLI / compatibility

- One new Alembic revision: `accounts.status` (server_default `'approved'`,
  backfills existing rows) + `account_credentials` + `sessions` +
  `email_tokens`.
- `create_key.py`, `init-test-key.sh`, and `gatekeep account`/`key` commands
  are **unchanged**: they create `approved` accounts without credentials, so
  operator bootstrap and the demo keep working.

## Testing

- **Unit:** password hash/verify; token issue/verify/expiry/single-use;
  session create/revoke; `console` and `smtp` backends (console captures the
  message body/link); service functions - signup email dedupe, wrong-password
  login, reset revokes sessions, approve sets status + budget, pending account
  blocked from key minting.
- **E2E (existing DB harness):** signup -> verify-email -> pending login shows
  pending state -> operator approve -> user login -> mint key -> that key
  authenticates a real gateway request. Email asserted via the console backend.
- **Frontend:** component tests for the new pages and cookie/CSRF behavior in
  `client.ts`.

## Security summary

- bcrypt passwords; SHA-256-hashed session/email tokens (raw values never
  stored).
- `HttpOnly` + `Secure` + `SameSite=Lax` session cookie; double-submit CSRF on
  cookie-authed mutations.
- No user enumeration on signup / login / reset-request.
- Per-IP rate limiting on unauthenticated auth endpoints.
- Session expiry + full session revocation on password reset.
- `require_approved` blocks pending/rejected/disabled accounts from key and API
  actions even when authenticated.
