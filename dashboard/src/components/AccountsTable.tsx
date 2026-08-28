import { useCallback, useEffect, useState } from "react";
import { getAccounts } from "../api/client";
import type { AccountStatsOut, MeResponse } from "../api/types";
import { formatUsd } from "../format";
import { useApiErrorHandler } from "../hooks/useApiErrorHandler";
import AccountDetailPanel from "./AccountDetailPanel";
import CreateAccountModal from "./CreateAccountModal";

interface AccountsTableProps {
  selfAccountId: number;
  /** The caller's own account status, carried through to the `MeResponse`
   * pushed by `onMeChanged` (this table's data has no status field of its
   * own to refresh it from). */
  selfStatus: string;
  onMeChanged: (me: MeResponse) => void;
  onUnauthorized: () => void;
}

/** Operator-only table of all accounts (name, budget, MTD spend, key count,
 * operator flag) with a Create button and a per-row Manage action. */
export default function AccountsTable({
  selfAccountId,
  selfStatus,
  onMeChanged,
  onUnauthorized,
}: AccountsTableProps) {
  const [accounts, setAccounts] = useState<AccountStatsOut[]>([]);
  const [selected, setSelected] = useState<AccountStatsOut | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const { error, setError, handleError } = useApiErrorHandler(onUnauthorized);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getAccounts();
      setAccounts(res.accounts);
      // Keep the open detail panel in sync with fresh data after a mutation.
      setSelected((cur) => (cur ? res.accounts.find((a) => a.id === cur.id) ?? null : null));
      // An operator managing their own row here mutates the same account
      // backing App's `me` state - push the fresh row up so BudgetCard and
      // the operator gate stay in sync instead of going stale.
      const self = res.accounts.find((a) => a.id === selfAccountId);
      if (self) {
        onMeChanged({
          account_id: self.id,
          name: self.name,
          is_operator: self.is_operator,
          monthly_budget_usd: self.monthly_budget_usd,
          spend_mtd: self.spend_mtd,
          status: selfStatus,
        });
      }
    } catch (err) {
      handleError(err, "Failed to load accounts");
    }
  }, [setError, handleError, onMeChanged, selfAccountId, selfStatus]);

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
