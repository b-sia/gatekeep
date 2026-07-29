import type { UsageBreakdownRow } from "../api/types";

interface BreakdownTableProps {
  title: string;
  rows: UsageBreakdownRow[];
}

/** A titled table of usage breakdown rows, with a relative cost bar per row
 * scaled against the row with the highest cost. */
export default function BreakdownTable({ title, rows }: BreakdownTableProps) {
  const maxCost = Math.max(1e-9, ...rows.map((row) => row.cost_usd));

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">{title}</h2>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Name</th>
            <th className="pb-2 text-right">Requests</th>
            <th className="pb-2 text-right">Tokens</th>
            <th className="pb-2 text-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-slate-800">
              <td className="py-2 pr-2">
                <div className="text-slate-200">{row.label ?? row.key}</div>
                <div className="h-1 w-full rounded bg-slate-800">
                  <div
                    className="h-1 rounded bg-indigo-500"
                    style={{ width: `${(row.cost_usd / maxCost) * 100}%` }}
                  />
                </div>
              </td>
              <td className="py-2 text-right text-slate-300">{row.request_count}</td>
              <td className="py-2 text-right text-slate-300">{row.total_tokens.toLocaleString()}</td>
              <td className="py-2 text-right text-slate-300">${row.cost_usd.toFixed(2)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={4} className="py-4 text-center text-slate-500">
                No data for this range.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
