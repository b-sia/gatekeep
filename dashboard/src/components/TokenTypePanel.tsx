import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimeseriesResponse } from "../api/types";
import { formatBucketLabel } from "../format";

interface TokenTypePanelProps {
  timeseries: TimeseriesResponse | null;
}

/** Grouped (not stacked) bar chart of input/output/cached tokens over time.
 *
 * Bars are grouped rather than stacked because `cached_tokens` is not
 * mutually exclusive with `prompt_tokens`/`completion_tokens`: a cache-hit
 * request's real token counts are already included in those two sums, so
 * stacking all three would double-count. Grouping avoids implying the three
 * bars sum to a combined total.
 */
export default function TokenTypePanel({ timeseries }: TokenTypePanelProps) {
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: formatBucketLabel(bucket.bucket_start, timeseries.interval),
      input: bucket.prompt_tokens,
      output: bucket.completion_tokens,
      cached: bucket.cached_tokens,
    })) ?? [];

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Token usage</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
              labelStyle={{ color: "#e2e8f0" }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="input" fill="#6366f1" name="Input tokens" />
            <Bar dataKey="output" fill="#f97316" name="Output tokens" />
            <Bar dataKey="cached" fill="#22d3ee" name="Cached tokens" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
