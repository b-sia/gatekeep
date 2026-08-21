import { useCallback, useEffect, useState } from "react";
import FilterBar, { type DashboardFilters } from "../components/FilterBar";
import StatRow from "../components/StatRow";
import UsageChart from "../components/UsageChart";
import ModelUsagePanel from "../components/ModelUsagePanel";
import TokenTypePanel from "../components/TokenTypePanel";
import SpendSavingsPanel from "../components/SpendSavingsPanel";
import LatencyPanel from "../components/LatencyPanel";
import LatencyByPathPanel from "../components/LatencyByPathPanel";
import BreakdownPanels from "../components/BreakdownPanels";
import PromptsPanel from "../components/PromptsPanel";
import EvalHistoryPanel from "../components/EvalHistoryPanel";
import {
  UnauthorizedError,
  getEvalHistory,
  getLatencySummary,
  getLatencyTimeseries,
  getPrompts,
  getUsageSummary,
  getUsageTimeseries,
  getUsageTimeseriesByModel,
} from "../api/client";
import { useApiErrorHandler } from "../hooks/useApiErrorHandler";
import type {
  EvalRunOut,
  LatencySummaryResponse,
  LatencyTimeseriesResponse,
  MeResponse,
  PromptOut,
  TimeseriesResponse,
  UsageByModelTimeseriesResponse,
  UsageSummaryResponse,
} from "../api/types";

interface DashboardPageProps {
  /** The caller's own account context (GET /me), or null while it loads.
   * Drives whether the operator-only prompts/eval panels are fetched and
   * shown - those routes are fleet-wide and gated to operators (see
   * `gatekeep/api/dashboard.py`), so a non-operator would only get 403s. */
  me: MeResponse | null;
  /** Set when the initial GET /me failed for a reason other than 401
   * (5xx, network error), so the operator-only section can offer a retry
   * instead of silently staying hidden forever. */
  meError: string | null;
  onRetryMe: () => void;
  /** Called when any dashboard API call comes back 401, so the app can drop
   * back to the API key entry screen and clear the stale stored key. */
  onUnauthorized: () => void;
}

/**
 * Top-level dashboard view: owns filter state, fetches usage/latency data
 * (and, for operators, fleet-wide prompt/eval data) for the current time
 * window and model filter, and renders the dashboard layout (header,
 * filters, stat cards, charts, breakdowns, and - for operators - prompts and
 * eval history).
 */
export default function DashboardPage({ me, meError, onRetryMe, onUnauthorized }: DashboardPageProps) {
  const isOperator = me?.is_operator ?? false;
  const [filters, setFilters] = useState<DashboardFilters>({
    rangeDays: 7,
    interval: "day",
    model: null,
  });
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [allModels, setAllModels] = useState<string[]>([]);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [byModel, setByModel] = useState<UsageByModelTimeseriesResponse | null>(null);
  const [latency, setLatency] = useState<LatencySummaryResponse | null>(null);
  const [latencySeries, setLatencySeries] = useState<LatencyTimeseriesResponse | null>(null);
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [prompts, setPrompts] = useState<PromptOut[]>([]);
  const { error, setError, handleError } = useApiErrorHandler(onUnauthorized);

  // Per-tenant usage/latency data doesn't depend on operator status at all
  // (see `gatekeep/api/dashboard.py`), so it's fetched independently of
  // `me`/`isOperator` - it shouldn't wait on, or be blanked by a failure of,
  // the unrelated GET /me call.
  const load = useCallback(async () => {
    setError(null);
    const end = new Date();
    const start = new Date(end.getTime() - filters.rangeDays * 24 * 60 * 60 * 1000);
    const windowParams = { start: start.toISOString(), end: end.toISOString() };
    try {
      const [summaryRes, timeseriesRes, byModelRes, latencyRes, latencySeriesRes] = await Promise.all([
        getUsageSummary({ ...windowParams, model: filters.model ?? undefined }),
        getUsageTimeseries({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
        getUsageTimeseriesByModel({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
        getLatencySummary({ ...windowParams, model: filters.model ?? undefined }),
        getLatencyTimeseries({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
      ]);
      setSummary(summaryRes);
      setTimeseries(timeseriesRes);
      setByModel(byModelRes);
      setLatency(latencyRes);
      setLatencySeries(latencySeriesRes);
    } catch (err) {
      handleError(err, "Failed to load dashboard data");
    }
  }, [filters, setError, handleError]);

  // Prompt/eval data is fleet-wide and operator-only on the backend, so a
  // non-operator would only get 403s - this only fires once `me` has
  // resolved with `is_operator: true`, and is fully independent of `load()`
  // above so it never duplicates the per-tenant fetches.
  const loadOperatorData = useCallback(async () => {
    try {
      const [evalsRes, promptsRes] = await Promise.all([getEvalHistory(), getPrompts()]);
      setRuns(evalsRes.runs);
      setPrompts(promptsRes.prompts);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      handleError(err, "Failed to load operator data");
    }
  }, [handleError, onUnauthorized]);

  // Fetch the model list from an *unfiltered* summary (no `model` param) so
  // the dropdown always lists every model seen in the current time window,
  // independent of whichever model is currently selected. Re-fetched only
  // when the time window changes, not on every model-filter change.
  const loadAllModels = useCallback(async () => {
    const end = new Date();
    const start = new Date(end.getTime() - filters.rangeDays * 24 * 60 * 60 * 1000);
    try {
      const res = await getUsageSummary({ start: start.toISOString(), end: end.toISOString() });
      setAllModels(res.by_model.map((row) => row.key));
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      // Non-fatal: the model dropdown just stays stale/empty until the next
      // successful window change. The main `load()` error banner already
      // covers the general "gateway is unreachable" case.
    }
  }, [filters.rangeDays, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadAllModels();
  }, [loadAllModels]);

  useEffect(() => {
    if (!isOperator) return;
    loadOperatorData();
  }, [isOperator, loadOperatorData]);

  return (
    <div>
      <FilterBar filters={filters} availableModels={allModels} onChange={setFilters} />
      {error && (
        <div className="mx-6 mt-4 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-300">
          <span>{error}</span>
          <button
            onClick={() => load()}
            className="rounded border border-red-800 px-3 py-1 text-xs text-red-200 hover:bg-red-900"
          >
            Retry
          </button>
        </div>
      )}
      <StatRow summary={summary} />
      <UsageChart timeseries={timeseries} />
      <ModelUsagePanel data={byModel} interval={filters.interval} />
      <TokenTypePanel timeseries={timeseries} />
      <SpendSavingsPanel timeseries={timeseries} />
      <LatencyPanel timeseries={latencySeries} summary={latency} />
      <LatencyByPathPanel summary={latency} />
      <BreakdownPanels summary={summary} latency={latency} />
      {isOperator && (
        <>
          <PromptsPanel prompts={prompts} onUnauthorized={onUnauthorized} />
          <EvalHistoryPanel runs={runs} />
        </>
      )}
      {!me && meError && (
        <div className="mx-6 mt-4 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-300">
          <span>Couldn't determine operator status - {meError}</span>
          <button
            onClick={onRetryMe}
            className="rounded border border-red-800 px-3 py-1 text-xs text-red-200 hover:bg-red-900"
          >
            Retry
          </button>
        </div>
      )}
    </div>
  );
}
