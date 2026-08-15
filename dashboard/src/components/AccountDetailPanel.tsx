import { useState } from "react";
import { patchAccount } from "../api/client";
import type { AccountStatsOut } from "../api/types";
import KeyTable from "./KeyTable";

interface AccountDetailPanelProps {
  account: AccountStatsOut;
  onClose: () => void;
  onChanged: () => void;
  onUnauthorized: () => void;
}

/** Operator detail panel for one account: rename, set/clear budget, toggle
 * operator, and manage that account's keys. Each action calls PATCH (or the
 * key routes) and refreshes the parent table on success. */
export default function AccountDetailPanel({
  account,
  onClose,
  onChanged,
  onUnauthorized,
}: AccountDetailPanelProps) {
  const [name, setName] = useState(account.name);
  const [budget, setBudget] = useState(
    account.monthly_budget_usd === null ? "" : String(account.monthly_budget_usd),
  );
  const [error, setError] = useState<string | null>(null);

  /** Runs one PATCH mutation, surfaces errors, and refreshes on success. */
  async function apply(body: Parameters<typeof patchAccount>[1]) {
    setError(null);
    try {
      await patchAccount(account.id, body);
      onChanged();
    } catch (err) {
      if (err instanceof Error) setError(err.message);
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 px-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-slate-800 bg-slate-900 p-6">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-100">Manage {account.name}</h2>
          <button
            onClick={onClose}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            Close
          </button>
        </div>
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}

        <div className="mb-4 flex items-end gap-2">
          <label className="flex-1 text-xs text-slate-400">
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
            />
          </label>
          <button
            onClick={() => apply({ name: name.trim() })}
            className="rounded bg-indigo-600 px-3 py-2 text-xs text-white hover:bg-indigo-500"
          >
            Rename
          </button>
        </div>

        <div className="mb-4 flex items-end gap-2">
          <label className="flex-1 text-xs text-slate-400">
            Monthly budget USD (blank = unlimited)
            <input
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
            />
          </label>
          <button
            onClick={() =>
              apply(
                budget.trim() === ""
                  ? { clear_budget: true }
                  : { monthly_budget_usd: Number(budget.trim()) },
              )
            }
            className="rounded bg-indigo-600 px-3 py-2 text-xs text-white hover:bg-indigo-500"
          >
            Save budget
          </button>
        </div>

        <div className="mb-4 flex items-center justify-between">
          <span className="text-sm text-slate-300">
            Operator: {account.is_operator ? "yes" : "no"}
          </span>
          <button
            onClick={() => apply({ is_operator: !account.is_operator })}
            className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            {account.is_operator ? "Revoke operator" : "Make operator"}
          </button>
        </div>

        <KeyTable accountId={account.id} onUnauthorized={onUnauthorized} />
      </div>
    </div>
  );
}
