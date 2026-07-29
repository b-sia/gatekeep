import type { UsageSummaryResponse } from "../api/types";

interface StatRowProps {
  summary: UsageSummaryResponse | null;
}

/** Formats a USD amount as `$X.XX`. */
function formatCost(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

/** Formats a token count with a `k`/`M` suffix for readability. */
function formatTokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}k`;
  return String(count);
}

/** A single stat tile showing a label, headline value, and supporting
 * context line. */
function StatCard({ label, value, context }: { label: string; value: string; context: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-100">{value}</div>
      <div className="mt-1 text-xs text-slate-500">{context}</div>
    </div>
  );
}

/** Row of headline stat tiles (requests, cost, tokens, savings, cache hit rate) for
 * the current filter selection. Renders loading placeholders until
 * `summary` is available. */
export default function StatRow({ summary }: StatRowProps) {
  if (!summary) {
    return (
      <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-5">
        {["Requests", "Total cost", "Total tokens", "Total savings", "Cache hit rate"].map(
          (label) => (
            <StatCard key={label} label={label} value="-" context="Loading..." />
          ),
        )}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-5">
      <StatCard
        label="Requests"
        value={summary.request_count.toLocaleString()}
        context={`${summary.cache_hit_count} cache hits`}
      />
      <StatCard label="Total cost" value={formatCost(summary.cost_usd)} context="Across all models" />
      <StatCard
        label="Total tokens"
        value={formatTokens(summary.total_tokens)}
        context={`${formatTokens(summary.prompt_tokens)} in / ${formatTokens(summary.completion_tokens)} out`}
      />
      <StatCard
        label="Total savings"
        value={formatCost(summary.savings_usd)}
        context={`${formatCost(summary.spend_usd)} spent`}
      />
      <StatCard
        label="Cache hit rate"
        value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`}
        context="Of total requests"
      />
    </div>
  );
}
