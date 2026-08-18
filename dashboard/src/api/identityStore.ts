/** A saved dashboard identity: a Gatekeep key plus the account context
 * resolved from `/me` when it was added. The roster is a list of these. */
export interface Identity {
  /** UUID roster handle; the value stored in a tab's active pointer. */
  id: string;
  /** Raw Gatekeep API key. */
  key: string;
  /** Account id from `/me` at add-time. Immutable for the entry's lifetime;
   * used to make sure a re-authenticate can't silently swap this entry onto
   * a different account. */
  accountId: number;
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
 * @param fields - The raw key and the account id/name/operator flag from
 *   `/me`.
 * @returns The created identity, including its generated id.
 */
export function addIdentity(fields: {
  key: string;
  accountId: number;
  accountName: string;
  isOperator: boolean;
}): Identity {
  const identity: Identity = {
    id: crypto.randomUUID(),
    key: fields.key,
    accountId: fields.accountId,
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
 * Replaces an entry's key with a freshly validated one, refreshes its
 * account name/operator flag to the values just resolved from `/me`, and
 * flips it back to `active`.
 *
 * @param id - The roster handle to re-authenticate.
 * @param newKey - The new, already-validated Gatekeep key.
 * @param accountId - The account id resolved from `/me` for `newKey`. Must
 *   match the entry's stored `accountId` - otherwise the pasted key belongs
 *   to a different account than the one this roster row represents, and
 *   accepting it would silently relabel the row.
 * @param accountName - The account name resolved from `/me` for `newKey`.
 * @param isOperator - The operator flag resolved from `/me` for `newKey`.
 * @throws {Error} If the entry no longer exists (e.g. another tab removed
 *   it after this picker loaded) or `accountId` does not match the entry's
 *   stored `accountId`. The roster is left unchanged in either case.
 */
export function reauthenticate(
  id: string,
  newKey: string,
  accountId: number,
  accountName: string,
  isOperator: boolean,
): void {
  const roster = readRoster();
  const entry = roster.find((candidate) => candidate.id === id);
  if (!entry) {
    throw new Error("This identity was removed - it may have been forgotten in another tab.");
  }
  if (entry.accountId !== accountId) {
    throw new Error("This key belongs to a different account");
  }
  writeRoster(
    roster.map((candidate) =>
      candidate.id === id
        ? { ...candidate, key: newKey, accountName, isOperator, status: "active" }
        : candidate,
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
