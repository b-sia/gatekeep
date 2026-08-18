import { useState, type FormEvent } from "react";
import {
  addIdentity,
  clearActiveIdentity,
  getActiveIdentity,
  listIdentities,
  reauthenticate,
  removeIdentity,
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

  /**
   * Selects an existing active identity for this tab. Guards against a
   * roster row that was invalidated by another tab between this picker's
   * mount and the click: if the pointer resolves to nothing, the dangling
   * pointer is cleared, the roster snapshot is refreshed so the row shows
   * as invalid, and the caller is not notified.
   */
  function activate(id: string) {
    setActiveIdentity(id);
    if (!getActiveIdentity()) {
      clearActiveIdentity();
      setRoster(listIdentities());
      setError(
        "That identity is no longer available - it may have been invalidated in another tab.",
      );
      return;
    }
    onIdentityActivated();
  }

  /**
   * Removes an identity from the roster entirely. If it happened to be this
   * tab's active identity, also clears this tab's active pointer, since the
   * identity would no longer exist to resolve to. If it was the entry
   * currently being re-authenticated, closes that flow too.
   */
  function forget(id: string) {
    if (getActiveIdentity()?.id === id) {
      clearActiveIdentity();
    }
    removeIdentity(id);
    setRoster(listIdentities());
    if (reauthId === id) {
      cancelReauth();
    }
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
        reauthenticate(reauthId, trimmed, me.account_id);
        setActiveIdentity(reauthId);
      } else {
        const created = addIdentity({
          key: trimmed,
          accountId: me.account_id,
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
                  <div className="flex items-center justify-between rounded border border-slate-700 px-3 py-2 text-sm text-slate-100 hover:bg-slate-800">
                    <button
                      onClick={() => activate(entry.id)}
                      className="flex flex-1 items-center gap-2 text-left"
                    >
                      <span>{entry.accountName}</span>
                      {entry.isOperator && (
                        <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-xs text-white">
                          operator
                        </span>
                      )}
                    </button>
                    <button
                      onClick={() => forget(entry.id)}
                      className="ml-2 rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
                    >
                      Forget
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center justify-between rounded border border-slate-800 px-3 py-2 text-sm text-slate-500">
                    <span className="flex items-center gap-2">
                      <span>
                        {entry.accountName}
                        <span className="ml-2 text-xs text-amber-500">key rejected</span>
                      </span>
                      {entry.isOperator && (
                        <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-xs text-white">
                          operator
                        </span>
                      )}
                    </span>
                    <div className="flex gap-2">
                      <button
                        onClick={() => startReauth(entry.id)}
                        className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
                      >
                        Re-authenticate
                      </button>
                      <button
                        onClick={() => forget(entry.id)}
                        className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
                      >
                        Forget
                      </button>
                    </div>
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
