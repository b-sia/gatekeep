# Dashboard Multi-Session Support - Design

Date: 2026-08-18
Status: Approved for planning
Scope: Frontend (`dashboard/`) only. Backend auth model is unchanged.

## Problem

The local dashboard supports only one logged-in identity per browser origin.
`client.ts` persists a single Gatekeep key in `localStorage` under a fixed key
(`gatekeep_dashboard_api_key`). Consequences:

- Two tabs of the dashboard share that one key; they cannot be different
  accounts at the same time.
- Pasting a different key clobbers the previous one - there is no roster of
  saved identities.

An account *is* its Gatekeep key: the backend is stateless and resolves the
bearer token to an `ApiKey` -> `Account` on every request
(`require_api_key`). There is no server-side session. Therefore multi-session
support is entirely a frontend storage/UX concern; no backend change is needed.

## Goal

Support requirement "C": a **shared roster of saved identities** plus
**per-tab isolation**, so two tabs can act as two different accounts
simultaneously, while the set of saved identities is shared across all tabs.

## Decisions (from brainstorming)

1. **New tab starts logged-out.** A fresh tab has no active identity and shows
   the identity picker; nothing is active until the user explicitly picks.
2. **Validate on add.** Adding an identity calls `GET /me` with the pasted key
   before saving; only valid keys enter the roster, labeled with the real
   account name and operator flag from `/me`.
3. **A rejected key is marked invalid, not dropped.** On a 401 the roster entry
   is flagged `invalid` and kept, with a Re-authenticate action; the active tab
   drops to the picker.
4. **Tabs are pinned.** Once a tab picks an identity it stays that identity for
   the tab's lifetime. Switching means log out -> picker (or open a new tab).
   No in-header identity switcher.
5. **Raw keys remain in `localStorage`.** This is the existing tradeoff for a
   local dashboard (anyone with the browser profile already has the key); a
   roster of several keys does not change the threat model.

## Architecture

### Two storage layers

The two requirements pull in opposite directions and therefore require two
physically distinct web-storage buckets:

- **Shared roster** -> `localStorage` (key `gatekeep_identities`), visible to
  every tab of the origin. Holds the saved identities.
- **Per-tab active pointer** -> `sessionStorage` (key
  `gatekeep_active_identity`), private to each tab. Holds only the `id` of the
  identity this tab is running as.

`sessionStorage` being per-tab is what delivers isolation; `localStorage` being
shared is what delivers the common roster. No single bucket is both, so the
split is load-bearing, not incidental.

### Identity store module (`src/api/identityStore.ts`, new)

All storage access is centralized here so `client.ts` and components never
touch `localStorage`/`sessionStorage` directly. This keeps identity/session
state (with its cross-bucket invariants) out of `client.ts` (an HTTP concern)
and gives the state a single testable surface.

```ts
interface Identity {
  id: string;          // uuid, the roster handle
  key: string;         // raw Gatekeep key
  accountName: string; // from /me at add-time
  isOperator: boolean; // from /me, drives the operator badge
  status: "active" | "invalid"; // "invalid" after a 401
}
```

Interface:

- `listIdentities(): Identity[]`
- `addIdentity(fields): Identity` - appends an `active` identity to the roster
  (caller has already validated via `/me`).
- `removeIdentity(id: string): void`
- `markInvalid(id: string): void` - flips one roster entry to `invalid`.
- `reauthenticate(id: string, newKey: string): void` - replaces the key and
  flips the entry back to `active`.
- `getActiveIdentity(): Identity | null` - reads the sessionStorage pointer and
  resolves it against the roster; returns `null` if the pointer is missing, the
  entry no longer exists, or the entry is `invalid`.
- `setActiveIdentity(id: string): void` - sets this tab's pointer.
- `clearActiveIdentity(): void` - logs this tab out (removes the pointer only).
- `getActiveKey(): string | null` - the key for this tab's active identity;
  what `client.ts` calls in place of `getStoredApiKey()`.

**Invariant:** the active pointer must resolve to a roster entry with status
`active`; `getActiveIdentity` enforces it by returning `null` otherwise.

### client.ts changes

- Remove `getStoredApiKey` / `setStoredApiKey` / `clearStoredApiKey` and the
  `STORAGE_KEY` constant.
- `request` and `mutate` read `getActiveKey()`; when it is `null`, throw
  `UnauthorizedError` (unchanged behavior, new source).
- On a 401 response: resolve the active identity's `id`, call
  `markInvalid(id)`, then throw `UnauthorizedError` (instead of clearing the
  single stored key).

## UI flows

### Identity picker (`src/components/IdentityPicker.tsx`, replaces `KeyEntryScreen`)

Shown whenever this tab has no active identity. Renders:

- **Roster list** - one row per identity: account name + operator badge.
  `invalid` rows are greyed and expose a **Re-authenticate** action. Clicking an
  `active` row calls `setActiveIdentity(id)` and the tab enters the dashboard as
  that identity.
- **Add identity** - the existing key input, reused. On submit, call `GET /me`
  with the pasted key *before* saving: valid -> `addIdentity(...)` using the
  name/operator flag from `/me`, then activate; invalid -> inline error, nothing
  saved.
- **Re-authenticate** (on an `invalid` row) - opens the key input scoped to that
  entry; a valid key calls `reauthenticate(id, newKey)` and activates it.

### Header (`src/components/Header.tsx`)

- Add a non-interactive identity indicator: account name + operator badge, so a
  tab always shows who it is.
- Repurpose the existing "API key" button as **Log out** ->
  `clearActiveIdentity()` -> back to the picker.
- No switcher dropdown (tabs are pinned). Roster management lives in the picker.

### App shell (`src/App.tsx`)

- Replace the `hasKey: boolean` gate with `activeIdentity: Identity | null`
  (seeded from `getActiveIdentity()`). Null -> picker; set -> dashboard.
- `handleUnauthorized` becomes "mark this identity invalid + clear this tab's
  pointer," then drop to the picker.

### End-to-end 401 flow

A request 401s -> `client.ts` calls `markInvalid(activeId)` and throws
`UnauthorizedError` -> App drops this tab to the picker -> the roster row for
that identity is now greyed with Re-authenticate. Other tabs running the same
identity find it `invalid` on their next call (via `getActiveIdentity` returning
`null`) and drop to the picker too - correct, since the key is dead everywhere.

## Testing

- **Add `vitest` + `jsdom`** (devDeps; standard for Vite) and a `"test"`
  script. This is the dashboard's first test setup.
- **Unit-test `identityStore`** against mock storage:
  - add/validate appends an `active` entry;
  - per-tab pointer isolation (two "tabs" over one roster resolve to different
    active identities);
  - `markInvalid` flips status; `getActiveIdentity` then returns `null` for that
    tab (the active-pointer-must-be-active invariant);
  - `reauthenticate` restores `active` and swaps the key;
  - `clearActiveIdentity` removes only the pointer, leaving the roster intact.
- **Manual E2E** for the UI flows (component-test harness is out of scope):
  open two tabs as two identities; revoke a key server-side; confirm both tabs
  drop to the picker with the entry greyed and re-authenticable.

## File-by-file change list

| File | Change |
|---|---|
| `src/api/identityStore.ts` | **New.** Roster + per-tab pointer; interface above. |
| `src/api/identityStore.test.ts` | **New.** Vitest unit tests for the store. |
| `src/api/client.ts` | Drop the single-key storage helpers; `request`/`mutate` use `getActiveKey()`; 401 calls `markInvalid`. |
| `src/components/IdentityPicker.tsx` | **New** (replaces `KeyEntryScreen.tsx`): roster list + add + re-auth. |
| `src/components/Header.tsx` | Identity indicator + badge; "API key" button -> "Log out". |
| `src/App.tsx` | `hasKey` -> `activeIdentity`; wire picker/logout/401 to the store. |
| `package.json` / `vitest.config.ts` | Add `vitest` + `jsdom`; `"test"` script. |

Backend: untouched.

## Out of scope

- Server-side sessions or any backend auth change.
- In-tab identity switching / header switcher (tabs are pinned).
- Encrypting keys at rest / master-password protection.
- Component/E2E test automation harness for the React UI.
