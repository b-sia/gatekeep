import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimeseriesResponse } from "../api/types";
import { formatBucketLabel } from "../format";

interface UsageChartProps {
  timeseries: TimeseriesResponse | null;
}

/** Stacked bar + line chart showing requests (split into cached/non-cached)
 * and cost over time, bucketed per the selected interval. */
export default function UsageChart({ timeseries }: UsageChartProps) {
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: formatBucketLabel(
        bucket.bucket_start,
        timeseries.interval as "minute" | "hour" | "day",
      ),
      nonCached: bucket.request_count - bucket.cache_hit_count,
      cached: bucket.cache_hit_count,
      cost: bucket.cost_usd,
    })) ?? [];

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Usage over time</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis yAxisId="left" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis yAxisId="right" orientation="right" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar yAxisId="left" dataKey="nonCached" stackId="requests" fill="#6366f1" name="Requests" />
            <Bar yAxisId="left" dataKey="cached" stackId="requests" fill="#22d3ee" name="Cache hits" />
            <Line
              yAxisId="right"
              type="monotone"
              dataKey="cost"
              stroke="#f59e0b"
              name="Cost (USD)"
              strokeWidth={2}
              dot={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
