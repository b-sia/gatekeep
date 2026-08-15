import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getAccountKeys, revokeKey } from "../api/client";
import type { KeyOut } from "../api/types";
import CreateKeyModal from "./CreateKeyModal";

interface KeyTableProps {
  accountId: number;
  onUnauthorized: () => void;
}

/** Lists an account's keys with a create button and per-row revoke. Revoked
 * keys stay listed, greyed out. Works for the caller's own account or, for an
 * operator, any account (the caller id is supplied by the parent). */
export default function KeyTable({ accountId, onUnauthorized }: KeyTableProps) {
  const [keys, setKeys] = useState<KeyOut[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getAccountKeys(accountId);
      setKeys(res.keys);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load keys");
    }
  }, [accountId, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  /** Revokes a key then reloads the table. */
  async function handleRevoke(keyId: number) {
    try {
      await revokeKey(accountId, keyId);
      await load();
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to revoke key");
    }
  }

  return (
    <section className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">API keys</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500"
        >
          Create key
        </button>
      </div>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            <th className="py-1">Name</th>
            <th className="py-1">Status</th>
            <th className="py-1">Created</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} className={k.active ? "text-slate-200" : "text-slate-600"}>
              <td className="py-1">{k.name}</td>
              <td className="py-1">{k.active ? "active" : "revoked"}</td>
              <td className="py-1">{new Date(k.created_at).toLocaleDateString()}</td>
              <td className="py-1 text-right">
                {k.active && (
                  <button
                    onClick={() => handleRevoke(k.id)}
                    className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
                  >
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {showCreate && (
        <CreateKeyModal
          accountId={accountId}
          onClose={() => setShowCreate(false)}
          onCreated={load}
        />
      )}
    </section>
  );
}
