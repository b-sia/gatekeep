import { useState } from "react";
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
import type { UsageByModelTimeseriesResponse } from "../api/types";
import { formatBucketLabel, formatUsd } from "../format";

type Metric = "tokens" | "requests" | "cost";

interface ModelUsagePanelProps {
  data: UsageByModelTimeseriesResponse | null;
  interval: "minute" | "hour" | "day";
}

type ChartRow = Record<string, string | number>;

const METRIC_LABELS: Record<Metric, string> = {
  tokens: "Tokens",
  requests: "Requests",
  cost: "Cost (USD)",
};

const MODEL_COLORS = ["#6366f1", "#f97316", "#22d3ee", "#a3e635", "#f472b6", "#facc15"];

/** Stacked bar chart of per-model usage over time. A metric toggle switches
 * which field feeds bar height (tokens / requests / cost) by re-pivoting
 * the already-fetched data client-side, without a re-fetch. */
export default function ModelUsagePanel({ data, interval }: ModelUsagePanelProps) {
  const [metric, setMetric] = useState<Metric>("tokens");

  const models = Array.from(new Set((data?.rows ?? []).map((row) => row.model))).sort();

  const byBucket = new Map<string, ChartRow>();
  for (const row of data?.rows ?? []) {
    if (!byBucket.has(row.bucket_start)) {
      const initial: ChartRow = { time: formatBucketLabel(row.bucket_start, interval) };
      for (const modelName of models) initial[modelName] = 0;
      byBucket.set(row.bucket_start, initial);
    }
    const existing = byBucket.get(row.bucket_start)!;
    const value =
      metric === "tokens" ? row.total_tokens : metric === "requests" ? row.request_count : row.cost_usd;
    existing[row.model] = (Number(existing[row.model]) || 0) + value;
  }
  const chartData = Array.from(byBucket.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([, row]) => row);

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium text-slate-300">Model usage</h2>
        <div className="flex gap-1">
          {(Object.keys(METRIC_LABELS) as Metric[]).map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className={`rounded px-2 py-1 text-xs ${
                metric === m ? "bg-indigo-600 text-white" : "text-slate-400 hover:bg-slate-800"
              }`}
            >
              {METRIC_LABELS[m]}
            </button>
          ))}
        </div>
      </div>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis
              tick={{ fill: "#94a3b8", fontSize: 12 }}
              tickFormatter={(value: number) => (metric === "cost" ? formatUsd(value) : String(value))}
            />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
              labelStyle={{ color: "#e2e8f0" }}
              formatter={(value: number) => (metric === "cost" ? formatUsd(value) : String(value))}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {models.map((modelName, i) => (
              <Bar
                key={modelName}
                dataKey={modelName}
                stackId="models"
                fill={MODEL_COLORS[i % MODEL_COLORS.length]}
                name={modelName}
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
