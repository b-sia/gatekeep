import { useCallback, useEffect, useState } from "react";
import { approveAccount, getPending, rejectAccount } from "../api/client";
import type { PendingAccountOut } from "../api/types";

/**
 * Operator-only panel listing self-serve signup requests awaiting approval.
 * Fetches the pending list on mount, renders one row per request with an
 * optional monthly budget input and Approve/Reject buttons, and re-fetches
 * the list after either action so an acted-on row disappears.
 */
export default function PendingRequestsPanel() {
  const [accounts, setAccounts] = useState<PendingAccountOut[]>([]);
  const [budgets, setBudgets] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getPending();
      setAccounts(res.accounts);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load pending requests");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleApprove = async (accountId: number) => {
    if (submitting.has(accountId)) return;
    const raw = budgets[accountId];
    if (raw !== undefined && raw !== "" && Number.isNaN(Number(raw))) {
      // A non-numeric budget must never silently become "unlimited" (which
      // is what `Number(raw)` -> `NaN` -> `JSON.stringify` -> `null` would
      // do). Block the approval instead of guessing intent.
      setError("Budget must be a number, or left blank for unlimited.");
      return;
    }
    const budget = raw === undefined || raw === "" ? null : Number(raw);
    setSubmitting((cur) => new Set(cur).add(accountId));
    try {
      await approveAccount(accountId, budget);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve request");
      setSubmitting((cur) => {
        const next = new Set(cur);
        next.delete(accountId);
        return next;
      });
    }
  };

  const handleReject = async (accountId: number) => {
    if (submitting.has(accountId)) return;
    setSubmitting((cur) => new Set(cur).add(accountId));
    try {
      await rejectAccount(accountId);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reject request");
      setSubmitting((cur) => {
        const next = new Set(cur);
        next.delete(accountId);
        return next;
      });
    }
  };

  if (accounts.length === 0 && !error) return null;

  return (
    <section className="mx-6 mt-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-200">Pending requests</h2>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            <th className="py-1">Account</th>
            <th className="py-1">Budget (USD)</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.account_id} className="text-slate-200">
              <td className="py-1">
                {a.name}
                {a.email !== a.name && (
                  <span className="ml-1 text-xs text-slate-500">({a.email})</span>
                )}
              </td>
              <td className="py-1">
                <input
                  type="number"
                  min={0}
                  placeholder="unlimited"
                  value={budgets[a.account_id] ?? ""}
                  onChange={(e) =>
                    setBudgets((cur) => ({ ...cur, [a.account_id]: e.target.value }))
                  }
                  className="w-24 rounded border border-slate-700 bg-slate-800 px-2 py-0.5 text-xs text-slate-200"
                />
              </td>
              <td className="py-1 text-right">
                <button
                  onClick={() => handleApprove(a.account_id)}
                  disabled={submitting.has(a.account_id)}
                  className="mr-2 rounded bg-indigo-600 px-2 py-0.5 text-xs text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {submitting.has(a.account_id) ? "Approving..." : "Approve"}
                </button>
                <button
                  onClick={() => handleReject(a.account_id)}
                  disabled={submitting.has(a.account_id)}
                  className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Reject
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
