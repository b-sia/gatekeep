import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LatencySummaryResponse } from "../api/types";
import { formatMs } from "../format";

interface LatencyByPathPanelProps {
  summary: LatencySummaryResponse | null;
}

// The stream row is time-to-last-token, not end-to-end: request_logs stores
// two different quantities in duration_ms depending on path, and this is the
// one panel where both appear side by side, so the label has to say so.
const PATH_LABELS: Record<string, string> = {
  cache_exact: "Exact cache hit",
  cache_semantic: "Semantic cache hit",
  provider: "Provider call",
  stream: "Stream (to last token)",
};

/** Horizontal p50/p95 bars per serving path, each labeled with its sample
 * count. This is what shows how much wall-clock time a cache hit actually
 * saves, which neither the usage panels nor Prometheus can show. */
export default function LatencyByPathPanel({ summary }: LatencyByPathPanelProps) {
  const rows = (summary?.by_path ?? [])
    .filter((row) => row.sample_count > 0)
    .map((row) => ({
      path: `${PATH_LABELS[row.key] ?? row.key} (n=${row.sample_count})`,
      p50: row.p50_ms,
      p95: row.p95_ms,
    }));

  return (
    <div className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-1 text-sm font-medium text-slate-300">Latency by path</h2>
      <p className="mb-3 text-xs text-slate-500">
        End-to-end per serving path. The stream row is request start to last token, a
        different quantity from the others.
      </p>
      <div className="h-72">
        {rows.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ left: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis
                type="number"
                tick={{ fill: "#94a3b8", fontSize: 12 }}
                tickFormatter={(value: number | null) => formatMs(value)}
              />
              <YAxis
                type="category"
                dataKey="path"
                width={180}
                tick={{ fill: "#94a3b8", fontSize: 12 }}
              />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
                labelStyle={{ color: "#e2e8f0" }}
                formatter={(value: number) => formatMs(value)}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="p50" fill="#6366f1" name="p50" />
              <Bar dataKey="p95" fill="#f97316" name="p95" />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-500">
            No latency samples for this range.
          </div>
        )}
      </div>
    </div>
  );
}
