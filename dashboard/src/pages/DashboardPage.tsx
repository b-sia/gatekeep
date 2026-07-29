import { useCallback, useEffect, useState } from "react";
import Header from "../components/Header";
import FilterBar, { type DashboardFilters } from "../components/FilterBar";
import StatRow from "../components/StatRow";
import UsageChart from "../components/UsageChart";
import BreakdownPanels from "../components/BreakdownPanels";
import PromptsPanel from "../components/PromptsPanel";
import EvalHistoryPanel from "../components/EvalHistoryPanel";
import {
  UnauthorizedError,
  getEvalHistory,
  getPrompts,
  getUsageSummary,
  getUsageTimeseries,
} from "../api/client";
import type { EvalRunOut, PromptOut, TimeseriesResponse, UsageSummaryResponse } from "../api/types";

interface DashboardPageProps {
  onUnauthorized: () => void;
}

export default function DashboardPage({ onUnauthorized }: DashboardPageProps) {
  const [filters, setFilters] = useState<DashboardFilters>({
    rangeDays: 7,
    interval: "day",
    model: null,
  });
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [prompts, setPrompts] = useState<PromptOut[]>([]);

  const load = useCallback(async () => {
    const end = new Date();
    const start = new Date(end.getTime() - filters.rangeDays * 24 * 60 * 60 * 1000);
    const windowParams = { start: start.toISOString(), end: end.toISOString() };
    try {
      const [summaryRes, timeseriesRes, evalsRes, promptsRes] = await Promise.all([
        getUsageSummary({ ...windowParams, model: filters.model ?? undefined }),
        getUsageTimeseries({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
        getEvalHistory(),
        getPrompts(),
      ]);
      setSummary(summaryRes);
      setTimeseries(timeseriesRes);
      setRuns(evalsRes.runs);
      setPrompts(promptsRes.prompts);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      throw err;
    }
  }, [filters, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  const availableModels = summary ? summary.by_model.map((row) => row.key) : [];

  return (
    <div className="min-h-screen bg-slate-950">
      <Header onClearKey={onUnauthorized} />
      <FilterBar filters={filters} availableModels={availableModels} onChange={setFilters} />
      <StatRow summary={summary} />
      <UsageChart timeseries={timeseries} />
      <BreakdownPanels summary={summary} />
      <PromptsPanel prompts={prompts} />
      <EvalHistoryPanel runs={runs} />
    </div>
  );
}
