# Dashboard Multi-Session Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let two browser tabs of the local dashboard act as two different Gatekeep accounts at once, backed by a shared roster of saved identities.

**Architecture:** A new `identityStore` module owns all web-storage access, splitting state across two buckets: a shared roster in `localStorage` (`gatekeep_identities`) and a per-tab active pointer in `sessionStorage` (`gatekeep_active_identity`). `client.ts` reads the active key through the store instead of a single localStorage key; on a 401 it marks the active identity invalid. The UI replaces the single key-entry screen with an identity picker (roster list + add + re-authenticate) and shows the active identity in the header.

**Tech Stack:** React 18 + TypeScript + Vite + Tailwind; new dev deps `vitest` + `jsdom` for the store's unit tests (the dashboard's first test setup).

**Spec:** `docs/superpowers/specs/2026-08-18-dashboard-multi-session-design.md`

## Global Constraints

- Frontend only. No backend/auth changes; `require_api_key` and `/dashboard/api/*` are untouched.
- JSDoc on every function, method, class, and interface (global CLAUDE.md rule).
- Never use the em dash character; use a plain `-`.
- Never auto-add an agent name as commit co-author.
- Raw keys stay in `localStorage` in plaintext (existing tradeoff; unchanged).
- The store is the ONLY module that touches `localStorage`/`sessionStorage`. `client.ts` and components go through it.
- Store invariant: the active pointer must resolve to a roster entry with status `active`; `getActiveIdentity()` returns `null` otherwise.
- All commands run from `dashboard/` (the frontend package root).

## File Structure

| File | Responsibility |
|---|---|
| `src/api/identityStore.ts` | New. Roster + per-tab pointer; the only storage-touching module. |
| `src/api/identityStore.test.ts` | New. Vitest unit tests for the store's invariants. |
| `src/api/client.ts` | Drop single-key helpers; add `validateKey`; `request`/`mutate` use `getActiveKey()`; 401 marks active identity invalid. |
| `src/components/IdentityPicker.tsx` | New. Replaces `KeyEntryScreen.tsx`: roster list + add + re-auth. |
| `src/components/KeyEntryScreen.tsx` | Deleted (replaced by IdentityPicker). |
| `src/components/Header.tsx` | Identity indicator + operator badge; "API key" button becomes "Log out". |
| `src/App.tsx` | `hasKey` gate becomes `activeIdentity`; wire picker/logout/401 to the store. |
| `package.json`, `vitest.config.ts` | Add `vitest` + `jsdom` dev deps and a `"test"` script. |

---

### Task 1: Test tooling (vitest + jsdom)

Sets up the dashboard's first test runner so later tasks can write and run unit tests.

**Files:**
- Modify: `dashboard/package.json`
- Create: `dashboard/vitest.config.ts`
- Create: `dashboard/src/api/smoke.test.ts` (temporary, deleted in Step 6)

**Interfaces:**
- Consumes: nothing.
- Produces: a working `npm test` command (vitest, jsdom environment). Later tasks rely on `npm test` running `*.test.ts` files under `src/` in a jsdom environment.

- [ ] **Step 1: Install dev dependencies**

Run: `npm install -D vitest@^2 jsdom@^24`
Expected: `vitest` and `jsdom` added to `devDependencies` in `package.json`.

- [ ] **Step 2: Add the test script**

In `dashboard/package.json`, add to the `"scripts"` object:

```json
    "test": "vitest run",
    "test:watch": "vitest"
```

- [ ] **Step 3: Create the vitest config**

Create `dashboard/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";

/** Vitest configuration for the dashboard's unit tests. Uses a jsdom
 * environment so tests can exercise browser-only globals such as
 * localStorage and sessionStorage. */
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
  },
});
```

- [ ] **Step 4: Add a temporary smoke test**

Create `dashboard/src/api/smoke.test.ts`:

```ts
import { describe, expect, it } from "vitest";

describe("test tooling", () => {
  it("runs in a jsdom environment with storage available", () => {
    localStorage.setItem("smoke", "ok");
    expect(localStorage.getItem("smoke")).toBe("ok");
    localStorage.clear();
  });
});
```

- [ ] **Step 5: Run the smoke test**

Run: `npm test`
Expected: PASS - 1 test passed, and the jsdom environment provides `localStorage`.

- [ ] **Step 6: Delete the smoke test and commit**

```bash
rm src/api/smoke.test.ts
git add package.json package-lock.json vitest.config.ts
git commit -m "chore(dashboard): add vitest + jsdom test tooling"
```

---

### Task 2: identityStore module

The heart of the change: a single module owning both storage buckets and the active-must-be-active invariant. Built test-first because it is the highest-risk surface.

**Files:**
- Create: `dashboard/src/api/identityStore.ts`
- Create: `dashboard/src/api/identityStore.test.ts`

**Interfaces:**
- Consumes: nothing (pure storage + logic).
- Produces:
  - `interface Identity { id: string; key: string; accountName: string; isOperator: boolean; status: "active" | "invalid"; }`
  - `listIdentities(): Identity[]`
  - `addIdentity(fields: { key: string; accountName: string; isOperator: boolean }): Identity`
  - `removeIdentity(id: string): void`
  - `markInvalid(id: string): void`
  - `reauthenticate(id: string, newKey: string): void`
  - `getActiveIdentity(): Identity | null`
  - `setActiveIdentity(id: string): void`
  - `clearActiveIdentity(): void`
  - `getActiveKey(): string | null`

- [ ] **Step 1: Write the failing tests**

Create `dashboard/src/api/identityStore.test.ts`:

```ts
import { beforeEach, describe, expect, it } from "vitest";
import {
  addIdentity,
  clearActiveIdentity,
  getActiveIdentity,
  getActiveKey,
  listIdentities,
  markInvalid,
  reauthenticate,
  removeIdentity,
  setActiveIdentity,
} from "./identityStore";

/** Fresh storage before every test so cases do not leak roster/pointer
 * state into one another. */
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe("addIdentity / listIdentities", () => {
  it("appends an active identity and returns it with a generated id", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    expect(created.id).toBeTruthy();
    expect(created.status).toBe("active");
    const roster = listIdentities();
    expect(roster).toHaveLength(1);
    expect(roster[0]).toEqual(created);
  });

  it("persists the roster in localStorage so other tabs can read it", () => {
    addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    const raw = localStorage.getItem("gatekeep_identities");
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw as string)).toHaveLength(1);
  });
});

describe("per-tab active pointer", () => {
  it("stores only the id in sessionStorage", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    expect(sessionStorage.getItem("gatekeep_active_identity")).toBe(created.id);
  });

  it("resolves the pointer to the roster entry", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    expect(getActiveIdentity()).toEqual(created);
    expect(getActiveKey()).toBe("sk-a");
  });

  it("returns null when no pointer is set (a fresh tab starts logged out)", () => {
    addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    expect(getActiveIdentity()).toBeNull();
    expect(getActiveKey()).toBeNull();
  });

  it("returns null when the pointed-to entry no longer exists", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    removeIdentity(created.id);
    expect(getActiveIdentity()).toBeNull();
  });
});

describe("markInvalid + the active-must-be-active invariant", () => {
  it("flips status to invalid but keeps the entry listed", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    markInvalid(created.id);
    const roster = listIdentities();
    expect(roster).toHaveLength(1);
    expect(roster[0].status).toBe("invalid");
  });

  it("makes getActiveIdentity return null for an invalid active entry", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    markInvalid(created.id);
    expect(getActiveIdentity()).toBeNull();
    expect(getActiveKey()).toBeNull();
  });
});

describe("reauthenticate", () => {
  it("swaps the key and restores active status", () => {
    const created = addIdentity({ key: "sk-old", accountName: "Alice", isOperator: false });
    markInvalid(created.id);
    reauthenticate(created.id, "sk-new");
    const roster = listIdentities();
    expect(roster[0].status).toBe("active");
    expect(roster[0].key).toBe("sk-new");
  });
});

describe("clearActiveIdentity", () => {
  it("removes only the pointer, leaving the roster intact", () => {
    const created = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    setActiveIdentity(created.id);
    clearActiveIdentity();
    expect(getActiveIdentity()).toBeNull();
    expect(listIdentities()).toHaveLength(1);
  });
});

describe("per-tab isolation over one shared roster", () => {
  it("lets two pointers over the same roster resolve to different identities", () => {
    const alice = addIdentity({ key: "sk-a", accountName: "Alice", isOperator: false });
    const bob = addIdentity({ key: "sk-b", accountName: "Bob", isOperator: true });

    // Tab 1 picks Alice.
    setActiveIdentity(alice.id);
    expect(getActiveKey()).toBe("sk-a");

    // Tab 2 is a different sessionStorage, simulated by overwriting the
    // pointer; the shared localStorage roster is unchanged.
    setActiveIdentity(bob.id);
    expect(getActiveKey()).toBe("sk-b");
    expect(listIdentities()).toHaveLength(2);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npm test`
Expected: FAIL - cannot resolve `./identityStore` / functions not defined.

- [ ] **Step 3: Implement the store**

Create `dashboard/src/api/identityStore.ts`:

```ts
/** A saved dashboard identity: a Gatekeep key plus the account context
 * resolved from `/me` when it was added. The roster is a list of these. */
export interface Identity {
  /** UUID roster handle; the value stored in a tab's active pointer. */
  id: string;
  /** Raw Gatekeep API key. */
  key: string;
  /** Account name from `/me` at add-time, for the roster/header label. */
  accountName: string;
  /** Operator flag from `/me`, drives the operator badge. */
  isOperator: boolean;
  /** `invalid` once a request with this key has been rejected with a 401. */
  status: "active" | "invalid";
}

/** localStorage key for the shared roster (visible to every tab). */
const ROSTER_KEY = "gatekeep_identities";
/** sessionStorage key for this tab's active-identity pointer (per-tab). */
const ACTIVE_KEY = "gatekeep_active_identity";

/**
 * Reads and parses the roster from localStorage.
 *
 * @returns The saved identities, or an empty array if none are stored or
 *   the stored value is missing/corrupt.
 */
function readRoster(): Identity[] {
  const raw = localStorage.getItem(ROSTER_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Identity[]) : [];
  } catch {
    return [];
  }
}

/**
 * Serializes and writes the roster to localStorage.
 *
 * @param roster - The identities to persist.
 */
function writeRoster(roster: Identity[]): void {
  localStorage.setItem(ROSTER_KEY, JSON.stringify(roster));
}

/**
 * Lists the saved identities shared across all tabs.
 *
 * @returns The roster, newest entries last.
 */
export function listIdentities(): Identity[] {
  return readRoster();
}

/**
 * Appends a new active identity to the shared roster. The caller must have
 * already validated the key via `/me`; the account context comes from there.
 *
 * @param fields - The raw key and the account name/operator flag from `/me`.
 * @returns The created identity, including its generated id.
 */
export function addIdentity(fields: {
  key: string;
  accountName: string;
  isOperator: boolean;
}): Identity {
  const identity: Identity = {
    id: crypto.randomUUID(),
    key: fields.key,
    accountName: fields.accountName,
    isOperator: fields.isOperator,
    status: "active",
  };
  writeRoster([...readRoster(), identity]);
  return identity;
}

/**
 * Removes an identity from the roster entirely.
 *
 * @param id - The roster handle to remove.
 */
export function removeIdentity(id: string): void {
  writeRoster(readRoster().filter((entry) => entry.id !== id));
}

/**
 * Flips one roster entry to `invalid` (e.g. after its key was rejected with
 * a 401). The entry stays listed so the user can re-authenticate it.
 *
 * @param id - The roster handle to invalidate.
 */
export function markInvalid(id: string): void {
  writeRoster(
    readRoster().map((entry) =>
      entry.id === id ? { ...entry, status: "invalid" } : entry,
    ),
  );
}

/**
 * Replaces an entry's key with a freshly validated one and flips it back to
 * `active`.
 *
 * @param id - The roster handle to re-authenticate.
 * @param newKey - The new, already-validated Gatekeep key.
 */
export function reauthenticate(id: string, newKey: string): void {
  writeRoster(
    readRoster().map((entry) =>
      entry.id === id ? { ...entry, key: newKey, status: "active" } : entry,
    ),
  );
}

/**
 * Resolves this tab's active pointer against the shared roster.
 *
 * @returns The active identity, or `null` if the pointer is missing, the
 *   entry no longer exists, or the entry is `invalid` (the
 *   active-must-be-active invariant).
 */
export function getActiveIdentity(): Identity | null {
  const id = sessionStorage.getItem(ACTIVE_KEY);
  if (!id) return null;
  const entry = readRoster().find((candidate) => candidate.id === id);
  if (!entry || entry.status !== "active") return null;
  return entry;
}

/**
 * Sets this tab's active pointer.
 *
 * @param id - The roster handle to make active for this tab.
 */
export function setActiveIdentity(id: string): void {
  sessionStorage.setItem(ACTIVE_KEY, id);
}

/**
 * Logs this tab out by removing its active pointer. The roster is untouched.
 */
export function clearActiveIdentity(): void {
  sessionStorage.removeItem(ACTIVE_KEY);
}

/**
 * The key for this tab's active identity, for `client.ts` to attach as the
 * bearer token.
 *
 * @returns The active identity's key, or `null` if this tab has no valid
 *   active identity.
 */
export function getActiveKey(): string | null {
  return getActiveIdentity()?.key ?? null;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npm test`
Expected: PASS - all identityStore tests green.

- [ ] **Step 5: Commit**

```bash
git add src/api/identityStore.ts src/api/identityStore.test.ts
git commit -m "feat(dashboard): add identityStore for multi-session roster + per-tab pointer"
```

---

### Task 3: client.ts - route auth through the store

Swaps the single-key helpers for the store, and adds `validateKey` so the picker can check a key via `/me` before saving.

**Files:**
- Modify: `dashboard/src/api/client.ts`

**Interfaces:**
- Consumes: `getActiveKey`, `getActiveIdentity`, `markInvalid` from `./identityStore` (Task 2); `MeResponse` from `./types`.
- Produces:
  - `validateKey(key: string): Promise<MeResponse>` - resolves with the account context for a raw key, throws `UnauthorizedError` on 401, throws `Error` otherwise.
  - `UnauthorizedError` (unchanged export).
  - `getStoredApiKey` / `setStoredApiKey` / `clearStoredApiKey` / `STORAGE_KEY` are removed.

- [ ] **Step 1: Remove the single-key helpers**

In `dashboard/src/api/client.ts`, delete the `STORAGE_KEY` constant and the `getStoredApiKey`, `setStoredApiKey`, and `clearStoredApiKey` functions (lines 20-36 in the current file).

- [ ] **Step 2: Import from the store and types**

At the top of `client.ts`, after the existing `types` import block, add:

```ts
import { getActiveIdentity, getActiveKey, markInvalid } from "./identityStore";
import type { MeResponse } from "./types";
```

Note: `MeResponse` is already imported in the existing `types` import block. If so, do NOT duplicate it - only add the `identityStore` import line.

- [ ] **Step 3: Update the `UnauthorizedError` doc comment**

Replace the `UnauthorizedError` JSDoc so it no longer claims to clear a stored key:

```ts
/** Thrown when a dashboard API request has no active identity, or the
 * gateway rejects the active key with a 401 (in which case that identity is
 * marked invalid in the roster as a side effect). */
export class UnauthorizedError extends Error {}
```

- [ ] **Step 4: Add a shared 401 handler and `validateKey`**

Below `errorMessage`, add:

```ts
/**
 * Marks this tab's active identity invalid (if one is set) and throws.
 * Called from `request`/`mutate` when the gateway returns 401 so the roster
 * reflects the dead key and the tab drops back to the picker.
 *
 * @throws {UnauthorizedError} Always.
 */
function handleRejectedKey(): never {
  const active = getActiveIdentity();
  if (active) markInvalid(active.id);
  throw new UnauthorizedError("API key was rejected");
}

/**
 * Validates a raw Gatekeep key by calling `GET /me` with it directly,
 * without touching the roster or the active pointer. Used by the identity
 * picker before a key is saved.
 *
 * @param key - The raw key to validate.
 * @returns The caller's account context if the key is accepted.
 * @throws {UnauthorizedError} If the gateway rejects the key with a 401.
 * @throws {Error} For any other non-OK response.
 */
export async function validateKey(key: string): Promise<MeResponse> {
  const url = new URL("/dashboard/api/me", window.location.origin);
  const response = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${key}` },
  });
  if (response.status === 401) {
    throw new UnauthorizedError("API key was rejected");
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response, "me"));
  }
  return response.json() as Promise<MeResponse>;
}
```

- [ ] **Step 5: Update `request` to use the active key**

In `request<T>`, replace the key lookup and the 401 branch:

```ts
  const apiKey = getActiveKey();
  if (!apiKey) {
    throw new UnauthorizedError("No active identity");
  }
```

and

```ts
  if (response.status === 401) {
    handleRejectedKey();
  }
```

- [ ] **Step 6: Update `mutate` the same way**

In `mutate<T>`, apply the identical two replacements: `getActiveKey()` with the `"No active identity"` message, and `handleRejectedKey()` in the 401 branch. Also update `request`'s and `mutate`'s JSDoc `@throws {UnauthorizedError}` lines to read "If no active identity is set, or the gateway responds 401 (that identity is marked invalid)."

- [ ] **Step 7: Typecheck**

Run: `npx tsc --noEmit`
Expected: PASS - no references to the removed helpers remain in `client.ts`. (Callers in other files are fixed in Tasks 4-6; if tsc reports errors there, they are expected until those tasks land. Confirm the only errors are the removed-import references in `App.tsx` / `KeyEntryScreen.tsx`, which the next tasks replace.)

- [ ] **Step 8: Commit**

```bash
git add src/api/client.ts
git commit -m "feat(dashboard): route client auth through identityStore + add validateKey"
```

---

### Task 4: IdentityPicker component

Replaces `KeyEntryScreen`: lists the roster, adds a validated identity, and re-authenticates invalid ones.

**Files:**
- Create: `dashboard/src/components/IdentityPicker.tsx`
- Delete: `dashboard/src/components/KeyEntryScreen.tsx`

**Interfaces:**
- Consumes: `listIdentities`, `addIdentity`, `reauthenticate`, `setActiveIdentity`, `type Identity` from `../api/identityStore`; `validateKey`, `UnauthorizedError` from `../api/client`.
- Produces: `IdentityPicker` default export with prop `onIdentityActivated: () => void`, called after an identity is set active for this tab.

- [ ] **Step 1: Create the component**

Create `dashboard/src/components/IdentityPicker.tsx`:

```tsx
import { useState, type FormEvent } from "react";
import {
  addIdentity,
  listIdentities,
  reauthenticate,
  setActiveIdentity,
  type Identity,
} from "../api/identityStore";
import { UnauthorizedError, validateKey } from "../api/client";

interface IdentityPickerProps {
  /** Called after an identity is set active for this tab, so the app shell
   * can leave the picker and load the dashboard. */
  onIdentityActivated: () => void;
}

/**
 * Logged-out screen for a tab: shows the shared roster of saved identities,
 * lets the user add a new one (validated via `/me` before saving), and lets
 * them re-authenticate an entry whose key was rejected. Picking an active
 * entry makes it this tab's identity.
 */
export default function IdentityPicker({ onIdentityActivated }: IdentityPickerProps) {
  const [roster, setRoster] = useState<Identity[]>(() => listIdentities());
  const [keyInput, setKeyInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The id of the invalid entry being re-authenticated, or null when the
  // input is adding a brand-new identity.
  const [reauthId, setReauthId] = useState<string | null>(null);

  /** Selects an existing active identity for this tab. */
  function activate(id: string) {
    setActiveIdentity(id);
    onIdentityActivated();
  }

  /**
   * Validates the entered key via `/me`, then either adds a new identity or
   * re-authenticates the entry named by `reauthId`, and activates it.
   */
  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = keyInput.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      const me = await validateKey(trimmed);
      if (reauthId) {
        reauthenticate(reauthId, trimmed);
        setActiveIdentity(reauthId);
      } else {
        const created = addIdentity({
          key: trimmed,
          accountName: me.name,
          isOperator: me.is_operator,
        });
        setActiveIdentity(created.id);
      }
      onIdentityActivated();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        setError("That key was rejected. Check it and try again.");
      } else {
        setError(err instanceof Error ? err.message : "Could not validate the key.");
      }
      setRoster(listIdentities());
    } finally {
      setBusy(false);
    }
  }

  /** Opens the key input scoped to re-authenticating one invalid entry. */
  function startReauth(id: string) {
    setReauthId(id);
    setKeyInput("");
    setError(null);
  }

  /** Returns to adding a new identity from a re-auth flow. */
  function cancelReauth() {
    setReauthId(null);
    setKeyInput("");
    setError(null);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
      <div className="w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6 shadow-xl">
        <h1 className="mb-1 text-lg font-semibold text-slate-100">Gatekeep</h1>
        <p className="mb-4 text-sm text-slate-400">
          Pick a saved identity or add one to view the dashboard.
        </p>

        {roster.length > 0 && (
          <ul className="mb-4 flex flex-col gap-2">
            {roster.map((entry) => (
              <li key={entry.id}>
                {entry.status === "active" ? (
                  <button
                    onClick={() => activate(entry.id)}
                    className="flex w-full items-center justify-between rounded border border-slate-700 px-3 py-2 text-left text-sm text-slate-100 hover:bg-slate-800"
                  >
                    <span>{entry.accountName}</span>
                    {entry.isOperator && (
                      <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-xs text-white">
                        operator
                      </span>
                    )}
                  </button>
                ) : (
                  <div className="flex items-center justify-between rounded border border-slate-800 px-3 py-2 text-sm text-slate-500">
                    <span>
                      {entry.accountName}
                      <span className="ml-2 text-xs text-amber-500">key rejected</span>
                    </span>
                    <button
                      onClick={() => startReauth(entry.id)}
                      className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
                    >
                      Re-authenticate
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}

        <form onSubmit={handleSubmit}>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
            {reauthId ? "Re-authenticate" : "Add identity"}
          </p>
          <input
            type="password"
            autoFocus
            value={keyInput}
            onChange={(event) => setKeyInput(event.target.value)}
            placeholder="sk-..."
            className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
          />
          {error && <p className="mb-3 text-xs text-red-400">{error}</p>}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy}
              className="flex-1 rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {busy ? "Checking..." : reauthId ? "Re-authenticate" : "Add and continue"}
            </button>
            {reauthId && (
              <button
                type="button"
                onClick={cancelReauth}
                className="rounded border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Delete the obsolete key-entry screen**

```bash
git rm src/components/KeyEntryScreen.tsx
```

- [ ] **Step 3: Typecheck**

Run: `npx tsc --noEmit`
Expected: The only remaining error is in `App.tsx`, which still imports `KeyEntryScreen` and the removed client helpers (fixed in Task 6). `IdentityPicker.tsx` itself typechecks.

- [ ] **Step 4: Commit**

```bash
git add src/components/IdentityPicker.tsx
git commit -m "feat(dashboard): add IdentityPicker, replacing KeyEntryScreen"
```

---

### Task 5: Header - identity indicator and Log out

Shows who a tab is, and repurposes the "API key" button as "Log out".

**Files:**
- Modify: `dashboard/src/components/Header.tsx`

**Interfaces:**
- Consumes: `type Identity` from `../api/identityStore`.
- Produces: `HeaderProps` gains `identity: Identity` and renames `onClearKey` to `onLogout`. `TabKey` export is unchanged.

- [ ] **Step 1: Update props and imports**

In `dashboard/src/components/Header.tsx`, add at the top:

```ts
import type { Identity } from "../api/identityStore";
```

Change the props interface to:

```ts
interface HeaderProps {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  /** The identity this tab is running as, shown in the indicator. */
  identity: Identity;
  /** Logs this tab out, returning it to the identity picker. */
  onLogout: () => void;
}
```

- [ ] **Step 2: Update the signature and doc comment**

Change the component signature to destructure the new props and update the JSDoc:

```tsx
/** Dashboard top bar: app title, an Analytics / Accounts & Keys tab control,
 * the active identity indicator, and a Log out button that returns this tab
 * to the identity picker. */
export default function Header({ activeTab, onTabChange, identity, onLogout }: HeaderProps) {
```

- [ ] **Step 3: Render the indicator and Log out button**

Replace the right-hand `<button>` (the "API key" button) with an identity indicator plus a Log out button:

```tsx
      <div className="flex items-center gap-3">
        <span className="flex items-center gap-2 text-sm text-slate-300">
          {identity.accountName}
          {identity.isOperator && (
            <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-xs text-white">
              operator
            </span>
          )}
        </span>
        <button
          onClick={onLogout}
          title="Log out of this tab"
          className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Log out
        </button>
      </div>
```

- [ ] **Step 4: Typecheck**

Run: `npx tsc --noEmit`
Expected: The only remaining error is in `App.tsx` (Task 6), which still passes the old `onClearKey` prop and lacks `identity`. `Header.tsx` itself typechecks.

- [ ] **Step 5: Commit**

```bash
git add src/components/Header.tsx
git commit -m "feat(dashboard): show active identity in header, replace API-key button with Log out"
```

---

### Task 6: App shell - wire the store, picker, and logout

Replaces the boolean key gate with an `activeIdentity` gate and connects every piece.

**Files:**
- Modify: `dashboard/src/App.tsx`

**Interfaces:**
- Consumes: `getActiveIdentity`, `clearActiveIdentity`, `type Identity` from `./api/identityStore`; `IdentityPicker`; the updated `Header` (`identity` + `onLogout`); `getMe` from `./api/client`.
- Produces: nothing downstream (root component).

- [ ] **Step 1: Replace imports**

In `dashboard/src/App.tsx`, replace the `KeyEntryScreen` and client-storage imports:

```ts
import IdentityPicker from "./components/IdentityPicker";
import Header, { type TabKey } from "./components/Header";
import DashboardPage from "./pages/DashboardPage";
import ManagementPage from "./pages/ManagementPage";
import { getMe } from "./api/client";
import { clearActiveIdentity, getActiveIdentity, type Identity } from "./api/identityStore";
import { useApiErrorHandler } from "./hooks/useApiErrorHandler";
import type { MeResponse } from "./api/types";
```

- [ ] **Step 2: Replace the `hasKey` state with `activeIdentity`**

```tsx
  const [activeIdentity, setActiveIdentity] = useState<Identity | null>(() =>
    getActiveIdentity(),
  );
  const [tab, setTab] = useState<TabKey>("analytics");
  const [me, setMe] = useState<MeResponse | null>(null);
```

- [ ] **Step 3: Rework `handleUnauthorized` and add a logout/activate pair**

```tsx
  /** Drops this tab back to the picker. `client.ts` has already marked the
   * rejected identity invalid on a 401; here we just clear this tab's
   * pointer and forget the loaded account. */
  const handleUnauthorized = useCallback(() => {
    clearActiveIdentity();
    setMe(null);
    setActiveIdentity(null);
  }, []);

  /** Re-reads the active identity after the picker sets one for this tab. */
  const handleIdentityActivated = useCallback(() => {
    setActiveIdentity(getActiveIdentity());
  }, []);
```

Note: `handleUnauthorized` doubles as the Log out handler (both clear the pointer and return to the picker); wire the header's `onLogout` to it.

- [ ] **Step 4: Update the `me` effect and the gate**

```tsx
  useEffect(() => {
    if (!activeIdentity) return;
    loadMe();
  }, [activeIdentity, loadMe]);

  if (!activeIdentity) {
    return <IdentityPicker onIdentityActivated={handleIdentityActivated} />;
  }
```

- [ ] **Step 5: Pass the new Header props**

```tsx
      <Header
        activeTab={tab}
        onTabChange={setTab}
        identity={activeIdentity}
        onLogout={handleUnauthorized}
      />
```

- [ ] **Step 6: Full typecheck and build**

Run: `npx tsc --noEmit && npm run build`
Expected: PASS - no type errors anywhere; the production build succeeds.

- [ ] **Step 7: Run the store tests once more**

Run: `npm test`
Expected: PASS - identityStore suite still green.

- [ ] **Step 8: Commit**

```bash
git add src/App.tsx
git commit -m "feat(dashboard): gate app on per-tab active identity via identityStore"
```

---

### Task 7: Manual end-to-end verification

No code; confirm the multi-session behavior in a real browser against a running gateway. If any check fails, fix inline (new failing test first where it belongs in the store) before finishing.

**Files:** none.

- [ ] **Step 1: Start the stack**

Start the gateway (`localhost:8100`) and the dashboard dev server (`npm run dev`) per the project's usual run steps. Have two valid Gatekeep keys ready (ideally one operator, one non-operator).

- [ ] **Step 2: Add two identities**

In tab 1, add key A - confirm it validates, shows the account name (and operator badge if applicable), and enters the dashboard. Log out. Add key B - confirm the roster now lists both A and B.

- [ ] **Step 3: Two tabs, two identities**

Open tab 2 (same origin). Confirm it starts logged out and shows the shared roster (both A and B). Pick A in tab 1 and B in tab 2. Confirm the header in each tab shows the correct account name, and each tab's data (e.g. Accounts & Keys) reflects its own account, simultaneously.

- [ ] **Step 4: Invalid-key flow**

Revoke key A server-side (or use the CLI). Trigger a request in tab 1 (switch tabs or reload a panel). Confirm tab 1 drops to the picker and the A row is greyed with "key rejected" and a Re-authenticate action. Confirm tab 2 (identity B) is unaffected.

- [ ] **Step 5: Re-authenticate**

Mint a fresh key for account A. In the picker, click Re-authenticate on the A row, paste the new key, confirm it validates and the tab enters the dashboard as A again with status restored to active in the roster.

- [ ] **Step 6: Fresh-tab isolation**

Open a third tab. Confirm it starts logged out (no identity auto-selected) even though the roster is populated - proving the per-tab pointer, not the shared roster, controls the active session.

- [ ] **Step 7: Record the result**

If all checks pass, the feature is complete. If anything failed, note it, fix inline (with a store-level regression test where applicable), and re-run the affected checks.
