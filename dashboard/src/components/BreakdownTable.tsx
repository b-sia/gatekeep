import type { LatencyBreakdownRow, UsageBreakdownRow } from "../api/types";
import { formatMs } from "../format";

interface BreakdownTableProps {
  title: string;
  rows: UsageBreakdownRow[];
  /** Latency rows for the same dimension, joined on `key`. Rows with no
   * latency samples render "-", never 0ms. Omit to hide the p95 column. */
  latencyRows?: LatencyBreakdownRow[];
}

/** A titled table of usage breakdown rows, with a relative cost bar per row
 * scaled against the row with the highest cost, and an optional p95 latency
 * column joined in on `key`. */
export default function BreakdownTable({ title, rows, latencyRows }: BreakdownTableProps) {
  const maxCost = Math.max(1e-9, ...rows.map((row) => row.cost_usd));
  const p95ByKey = new Map(
    (latencyRows ?? []).map((row) => [row.key, row.sample_count > 0 ? row.p95_ms : null]),
  );
  const showLatency = latencyRows !== undefined;
  const columnCount = showLatency ? 5 : 4;

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
            {showLatency && (
              <th className="pb-2 text-right" title="Non-streaming requests only">
                p95
              </th>
            )}
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
              {showLatency && (
                <td className="py-2 text-right text-slate-300">
                  {formatMs(p95ByKey.get(row.key) ?? null)}
                </td>
              )}
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={columnCount} className="py-4 text-center text-slate-500">
                No data for this range.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
