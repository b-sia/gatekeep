import BreakdownTable from "./BreakdownTable";
import type { UsageSummaryResponse } from "../api/types";

interface BreakdownPanelsProps {
  summary: UsageSummaryResponse | null;
}

export default function BreakdownPanels({ summary }: BreakdownPanelsProps) {
  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 lg:grid-cols-3">
      <BreakdownTable title="Cost by model" rows={summary?.by_model ?? []} />
      <BreakdownTable title="Cost by API key" rows={summary?.by_key ?? []} />
      <BreakdownTable title="Cost by prompt" rows={summary?.by_prompt ?? []} />
    </div>
  );
}
