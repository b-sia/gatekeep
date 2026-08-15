import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getAccounts } from "../api/client";
import type { AccountStatsOut } from "../api/types";
import { formatUsd } from "../format";
import AccountDetailPanel from "./AccountDetailPanel";
import CreateAccountModal from "./CreateAccountModal";

interface AccountsTableProps {
  onUnauthorized: () => void;
}

/** Operator-only table of all accounts (name, budget, MTD spend, key count,
 * operator flag) with a Create button and a per-row Manage action. */
export default function AccountsTable({ onUnauthorized }: AccountsTableProps) {
  const [accounts, setAccounts] = useState<AccountStatsOut[]>([]);
  const [selected, setSelected] = useState<AccountStatsOut | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getAccounts();
      setAccounts(res.accounts);
      // Keep the open detail panel in sync with fresh data after a mutation.
      setSelected((cur) => (cur ? res.accounts.find((a) => a.id === cur.id) ?? null : null));
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load accounts");
    }
  }, [onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="mx-6 mt-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">All accounts</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500"
        >
          Create account
        </button>
      </div>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-slate-500">
          <tr>
            <th className="py-1">Name</th>
            <th className="py-1">Budget</th>
            <th className="py-1">Spend (MTD)</th>
            <th className="py-1">Keys</th>
            <th className="py-1">Operator</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id} className="text-slate-200">
              <td className="py-1">{a.name}</td>
              <td className="py-1">
                {a.monthly_budget_usd === null ? "unlimited" : formatUsd(a.monthly_budget_usd)}
              </td>
              <td className="py-1">{formatUsd(a.spend_mtd)}</td>
              <td className="py-1">
                {a.active_key_count} active / {a.total_key_count} total
              </td>
              <td className="py-1">{a.is_operator ? "yes" : "no"}</td>
              <td className="py-1 text-right">
                <button
                  onClick={() => setSelected(a)}
                  className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
                >
                  Manage &rsaquo;
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {showCreate && (
        <CreateAccountModal
          onClose={() => setShowCreate(false)}
          onCreated={load}
          onUnauthorized={onUnauthorized}
        />
      )}
      {selected && (
        <AccountDetailPanel
          account={selected}
          onClose={() => setSelected(null)}
          onChanged={load}
          onUnauthorized={onUnauthorized}
        />
      )}
    </section>
  );
}
