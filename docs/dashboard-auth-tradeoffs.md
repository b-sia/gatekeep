# Dashboard Auth: API Key in `localStorage`

Notes on how the dashboard's authentication works today, and the tradeoffs
of that design. Written as a finding to revisit later - not an active
design doc.

## How it works

The dashboard has no login system of its own. It reuses the same API key
used for `curl`-ing `/v1/chat/completions`. The flow, across
`dashboard/src/api/client.ts`, `KeyEntryScreen.tsx`, and `App.tsx`:

1. **On load**, `App.tsx` calls `getStoredApiKey()`, which reads
   `localStorage.getItem("gatekeep_dashboard_api_key")`. If nothing's
   there, it renders `KeyEntryScreen` instead of the dashboard.
2. **The user pastes a key** into that screen; on submit,
   `setStoredApiKey(key)` does a plain `localStorage.setItem(...)`, and the
   app switches to `DashboardPage`.
3. **Every API call** goes through `client.ts`'s `request()` helper, which
   reads the stored key and attaches it as `Authorization: Bearer <key>` on
   each fetch to `/dashboard/api/*`. On the backend, this hits the exact
   same `require_api_key` dependency every other Gatekeep endpoint uses -
   there's no separate dashboard-auth code path at all.
4. **If the server ever returns 401** (key revoked, expired, wrong),
   `client.ts` calls `clearStoredApiKey()` (removes it from `localStorage`)
   and the app drops back to the entry screen automatically.
5. A small "API key" button in the header lets the user manually
   clear/replace it at any time, via the same mechanism.

So `localStorage` here is just a persistence layer for "the Bearer token
the user already has," not a session store, not a cookie, not a JWT - it's
the identical credential a script or SDK client would use, just typed into
a form once instead of hardcoded in an env var.

## Upsides

- **Zero new auth surface.** No session table, no cookie handling, no CSRF
  concerns (bearer tokens in a header aren't auto-attached by the browser
  to cross-origin requests the way cookies are), no token-refresh logic to
  build or maintain.
- **One security model, not two.** A dashboard key and a `curl` key are
  literally the same object with the same privileges - no separate "is
  this credential more or less trusted" question for anyone auditing the
  system.
- **Persists across reloads/restarts** with no extra work - good UX for a
  tool opened repeatedly, without "remember me" checkboxes or refresh
  tokens.
- **Trivially simple to reason about and revoke.** Rotating/deleting the
  `ApiKey` row on the backend instantly invalidates dashboard access too,
  via the same mechanism that already invalidates any other client using
  that key.

## Tradeoffs

- **XSS blast radius is larger than it needs to be.** Anything in
  `localStorage` is readable by any JS running on that origin. If the
  dashboard (or a future dependency) ever had an XSS bug, an attacker gets
  the *raw, full-privilege* API key - not a scoped "read dashboard data
  only" token. A proper session with an `httpOnly` cookie can't be read by
  JS even under XSS; this design trades that protection away.
- **No scoping.** The key that reads the dashboard is the same key that can
  spend money via `/v1/chat/completions`. There's no way to grant someone
  "dashboard viewer" access without also granting full API access. This was
  a deliberate simplicity tradeoff in the original design (avoid inventing
  a second credential type), not an oversight - but it's the sharpest edge
  of this approach.
- **No expiry.** The key sits in `localStorage` indefinitely until cleared,
  the browser storage is wiped, or the key is revoked server-side. No
  idle-timeout or session lifetime.
- **Per-browser, not per-user.** Doesn't sync across devices/browsers - the
  user re-enters it on each one. Minor UX cost, arguably a small isolation
  benefit too.
- **Physical/device compromise risk.** If someone gets local access to an
  unlocked browser profile with the key already stored, they have full API
  access with no additional secret needed (same risk class as any
  bearer-token-in-storage scheme, not unique to this build).

## Why this was still the right call (for now)

Gatekeep is a self-hosted, typically internal tool, and every other client
already authenticates this way. Inventing a scoped, expiring
dashboard-only credential would have meant building a second auth
subsystem just for this one surface - ruled out as unnecessary complexity
for the current threat model in the original dashboard design spec
(`docs/superpowers/specs/2026-07-28-dashboard-redesign-design.md`, §3).

## To revisit if

- The dashboard is ever exposed beyond a trusted network.
- A need emerges for read-only/viewer-only access distinct from full API
  access.
- Any XSS-capable dependency gets introduced into `dashboard/`.
