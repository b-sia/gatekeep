import BreakdownTable from "./BreakdownTable";
import type { LatencySummaryResponse, UsageSummaryResponse } from "../api/types";

interface BreakdownPanelsProps {
  summary: UsageSummaryResponse | null;
  latency: LatencySummaryResponse | null;
}

/** Renders the three usage breakdown tables (by model, by API key, by
 * prompt) side by side, each carrying a p95 latency column joined from the
 * latency summary. The p95 figures cover non-streaming requests only,
 * matching the rest of the latency surface. */
export default function BreakdownPanels({ summary, latency }: BreakdownPanelsProps) {
  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 lg:grid-cols-3">
      <BreakdownTable
        title="Cost by model"
        rows={summary?.by_model ?? []}
        latencyRows={latency?.by_model ?? []}
      />
      <BreakdownTable
        title="Cost by API key"
        rows={summary?.by_key ?? []}
        latencyRows={latency?.by_key ?? []}
      />
      <BreakdownTable
        title="Cost by prompt"
        rows={summary?.by_prompt ?? []}
        latencyRows={latency?.by_prompt ?? []}
      />
    </div>
  );
}
