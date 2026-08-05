import { useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  LatencySummaryResponse,
  LatencyTimeseriesBucket,
  LatencyTimeseriesResponse,
} from "../api/types";
import { formatBucketLabel, formatMs } from "../format";

type Metric = "e2e" | "provider" | "overhead" | "ttft";

interface LatencyPanelProps {
  timeseries: LatencyTimeseriesResponse | null;
  summary: LatencySummaryResponse | null;
}

const METRIC_LABELS: Record<Metric, string> = {
  e2e: "End-to-end",
  provider: "Provider",
  overhead: "Gateway overhead",
  ttft: "TTFT",
};

// Panel copy has to state what is measured, or the gap against Grafana gets
// reported as a bug: request_logs.duration_ms stops just before the
// accounting write, so it excludes JSON serialization and the socket write
// and reads slightly lower than gatekeep_request_duration_seconds.
const METRIC_NOTES: Record<Metric, string> = {
  e2e: "Non-streaming paths only. Measured from request start to just before the accounting write, so it reads slightly below the Prometheus end-to-end span.",
  provider: "Non-streaming paths only. Cache hits made no upstream call and are excluded.",
  overhead: "Non-streaming paths only. On a cache hit the entire duration is gateway time.",
  ttft: "Streamed requests only.",
};

/** One compact label/value pair in the panel header's stat strip. */
function HeaderStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-right">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-sm font-medium text-slate-200">{value}</div>
    </div>
  );
}

/**
 * p50/p95 latency over time, with a metric toggle (end-to-end / provider /
 * gateway overhead / TTFT) that re-reads already-fetched data client-side
 * rather than refetching, and a header stat strip of window-wide figures.
 */
export default function LatencyPanel({ timeseries, summary }: LatencyPanelProps) {
  const [metric, setMetric] = useState<Metric>("e2e");

  const p50Key = `${metric}_p50_ms` as keyof LatencyTimeseriesBucket;
  const p95Key = `${metric}_p95_ms` as keyof LatencyTimeseriesBucket;
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: formatBucketLabel(bucket.bucket_start, timeseries.interval),
      p50: bucket[p50Key] as number | null,
      p95: bucket[p95Key] as number | null,
    })) ?? [];
  const hasSamples = data.some((row) => row.p50 !== null || row.p95 !== null);

  return (
    <div className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-slate-300">Latency</h2>
        <div className="flex items-center gap-4">
          <HeaderStat label="p50 e2e" value={formatMs(summary?.e2e_ms?.p50_ms)} />
          <HeaderStat label="p95 e2e" value={formatMs(summary?.e2e_ms?.p95_ms)} />
          <HeaderStat label="p95 TTFT" value={formatMs(summary?.ttft_ms?.p95_ms)} />
          <HeaderStat label="p50 overhead" value={formatMs(summary?.overhead_ms?.p50_ms)} />
        </div>
      </div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-2xl text-xs text-slate-500">{METRIC_NOTES[metric]}</p>
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
        {hasSamples ? (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
              <YAxis
                tick={{ fill: "#94a3b8", fontSize: 12 }}
                tickFormatter={(value: number) => formatMs(value)}
              />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
                labelStyle={{ color: "#e2e8f0" }}
                formatter={(value: number) => formatMs(value)}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line
                type="monotone"
                dataKey="p50"
                stroke="#6366f1"
                name="p50"
                strokeWidth={2}
                dot={false}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="p95"
                stroke="#f97316"
                name="p95"
                strokeWidth={2}
                dot={false}
                connectNulls
              />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-500">
            No {METRIC_LABELS[metric].toLowerCase()} samples for this range.
          </div>
        )}
      </div>
    </div>
  );
}
