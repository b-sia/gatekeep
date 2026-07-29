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

interface SpendSavingsPanelProps {
  timeseries: TimeseriesResponse | null;
}

/** Stacked bar chart of actual spend vs. cache savings over time. Spend and
 * savings are mutually exclusive per bucket (split by the `cached` flag),
 * so stacking them sums to that bucket's total cost. */
export default function SpendSavingsPanel({ timeseries }: SpendSavingsPanelProps) {
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: new Date(bucket.bucket_start).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: timeseries.interval !== "day" ? "numeric" : undefined,
        minute: timeseries.interval === "minute" ? "numeric" : undefined,
      }),
      spend: bucket.spend_usd,
      savings: bucket.savings_usd,
    })) ?? [];

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Spend vs. savings</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis
              tick={{ fill: "#94a3b8", fontSize: 12 }}
              tickFormatter={(value: number) => `$${value.toFixed(2)}`}
            />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
              formatter={(value: number) => `$${value.toFixed(2)}`}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="spend" stackId="cost" fill="#f97316" name="Spend" />
            <Bar dataKey="savings" stackId="cost" fill="#22d3ee" name="Savings" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
