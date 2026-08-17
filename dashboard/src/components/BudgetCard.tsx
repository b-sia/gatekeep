import { formatUsd } from "../format";
import type { MeResponse } from "../api/types";

interface BudgetCardProps {
  me: MeResponse | null;
}

/** View-only card showing the caller's monthly budget cap and live
 * month-to-date budget-relevant spend (from GET /me). Tenants cannot change
 * their own cap, so there is no edit control here. */
export default function BudgetCard({ me }: BudgetCardProps) {
  if (!me) return null;
  const cap = me.monthly_budget_usd;
  const pct = cap && cap > 0 ? Math.min(100, (me.spend_mtd / cap) * 100) : null;

  return (
    <section className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Budget (this month)</h2>
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold text-slate-100">{formatUsd(me.spend_mtd)}</span>
        <span className="text-sm text-slate-400">
          {cap === null ? "of unlimited" : `of ${formatUsd(cap)}`}
        </span>
      </div>
      {pct !== null && (
        <div className="mt-3 h-2 w-full overflow-hidden rounded bg-slate-800">
          <div
            className={pct >= 100 ? "h-full bg-red-500" : "h-full bg-indigo-500"}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      <p className="mt-2 text-xs text-slate-500">
        Budget-relevant spend (provider cost, cache hits excluded).
      </p>
    </section>
  );
}
