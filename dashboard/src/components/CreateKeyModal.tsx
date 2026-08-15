import { useState } from "react";
import { createKey } from "../api/client";

interface CreateKeyModalProps {
  accountId: number;
  onClose: () => void;
  onCreated: () => void;
}

/**
 * Two-step key mint flow:
 *  1. Name the key.
 *  2. Show the raw key exactly once with a copy button and a can't-undo
 *     warning, gated behind an "I've saved it" checkbox before it can close.
 */
export default function CreateKeyModal({ accountId, onClose, onCreated }: CreateKeyModalProps) {
  const [name, setName] = useState("");
  const [rawKey, setRawKey] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Submits step 1: mints the key and advances to the show-once panel. */
  async function handleCreate() {
    setError(null);
    setBusy(true);
    try {
      const created = await createKey(accountId, name.trim());
      setRawKey(created.key);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create key");
    } finally {
      setBusy(false);
    }
  }

  /** Closes the show-once panel and notifies the parent to refresh. */
  function handleDone() {
    onCreated();
    onClose();
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 p-6">
        {rawKey === null ? (
          <>
            <h2 className="mb-3 text-base font-semibold text-slate-100">Create API key</h2>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="key name (e.g. prod)"
              className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
            />
            {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
            <div className="flex justify-end gap-2">
              <button
                onClick={onClose}
                className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={busy || name.trim() === ""}
                className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                Create
              </button>
            </div>
          </>
        ) : (
          <>
            <h2 className="mb-2 text-base font-semibold text-slate-100">Save your API key</h2>
            <p className="mb-3 text-sm text-amber-400">
              This is shown once and cannot be recovered. Copy it now.
            </p>
            <div className="mb-3 flex items-center gap-2">
              <code className="flex-1 overflow-x-auto rounded border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100">
                {rawKey}
              </code>
              <button
                onClick={() => navigator.clipboard.writeText(rawKey)}
                className="rounded border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800"
              >
                Copy
              </button>
            </div>
            <label className="mb-4 flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              I&apos;ve saved it somewhere safe
            </label>
            <div className="flex justify-end">
              <button
                onClick={handleDone}
                disabled={!confirmed}
                className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                Done
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
