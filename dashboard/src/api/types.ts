/** A single row in a usage breakdown (by model, by API key, or by prompt). */
export interface UsageBreakdownRow {
  key: string;
  label?: string | null;
  request_count: number;
  total_tokens: number;
  cost_usd: number;
  cache_hit_count: number;
}

/** Aggregate usage/cost totals for a time window, plus breakdowns by model,
 * API key, and prompt. */
export interface UsageSummaryResponse {
  start: string;
  end: string;
  request_count: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  spend_usd: number;
  savings_usd: number;
  cache_hit_count: number;
  cache_hit_rate: number;
  by_model: UsageBreakdownRow[];
  by_key: UsageBreakdownRow[];
  by_prompt: UsageBreakdownRow[];
}

/** One bucket of a usage timeseries (requests/cache hits/cost/tokens within
 * a single minute, hour, or day interval). */
export interface TimeseriesBucket {
  bucket_start: string;
  request_count: number;
  cache_hit_count: number;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  spend_usd: number;
  savings_usd: number;
}

/** Usage broken into fixed-size time buckets over a window, for charting. */
export interface TimeseriesResponse {
  start: string;
  end: string;
  interval: "minute" | "hour" | "day";
  buckets: TimeseriesBucket[];
}

/** One (time bucket, model) row of request/token/cost totals. */
export interface UsageByModelBucket {
  bucket_start: string;
  model: string;
  request_count: number;
  total_tokens: number;
  cost_usd: number;
}

/** Usage bucketed by both time and model, as a flat list of rows - group by
 * `model` client-side to build per-model chart series. */
export interface UsageByModelTimeseriesResponse {
  start: string;
  end: string;
  interval: "minute" | "hour" | "day";
  rows: UsageByModelBucket[];
}

/** Result of a single eval suite run against a prompt version. */
export interface EvalRunOut {
  id: number;
  suite_id: number;
  prompt_name: string;
  prompt_version_id: number;
  version_num: number;
  model: string;
  score: number;
  passed: boolean;
  created_at: string;
}

/** A list of eval runs, most recent first. */
export interface EvalHistoryResponse {
  runs: EvalRunOut[];
}

/** A registered prompt and its currently active version. */
export interface PromptOut {
  name: string;
  active_version_num: number | null;
  created_at: string;
  updated_at: string;
}

/** A list of registered prompts. */
export interface PromptListResponse {
  prompts: PromptOut[];
}

/** A single version in a prompt's edit/promotion history. */
export interface PromptVersionOut {
  version_num: number;
  active: boolean;
  created_at: string;
  created_by: string | null;
  notes: string | null;
}

/** Full version timeline for one prompt. */
export interface PromptVersionTimelineResponse {
  name: string;
  versions: PromptVersionOut[];
}

/** p50/p95/p99 of one latency quantity, in milliseconds. */
export interface Percentiles {
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
}

/** One row of a latency breakdown (by path, model, API key, or prompt).
 * `p50_ms`/`p95_ms` are null when the group has no qualifying samples, so
 * render "-" rather than 0ms. */
export interface LatencyBreakdownRow {
  key: string;
  label?: string | null;
  sample_count: number;
  p50_ms: number | null;
  p95_ms: number | null;
}

/** Latency percentiles for a time window, plus breakdowns.
 *
 * `e2e_ms`, `provider_ms`, and `overhead_ms` cover the non-streaming paths
 * only; `stream_ttlt_ms` and `ttft_ms` cover the streaming one. `duration_ms`
 * in the database means end-to-end on one side and time-to-last-token on the
 * other, so the two are never blended. `sample_count` counts every
 * latency-eligible row regardless of path. */
export interface LatencySummaryResponse {
  start: string;
  end: string;
  sample_count: number;
  e2e_ms: Percentiles | null;
  provider_ms: Percentiles | null;
  overhead_ms: Percentiles | null;
  stream_ttlt_ms: Percentiles | null;
  ttft_ms: Percentiles | null;
  by_path: LatencyBreakdownRow[];
  by_model: LatencyBreakdownRow[];
  by_key: LatencyBreakdownRow[];
  by_prompt: LatencyBreakdownRow[];
}

/** One bucket of latency percentiles. The `e2e`/`provider`/`overhead`
 * fields are non-streaming; `ttft` is streaming. Any field is null when
 * that bucket had no qualifying rows. */
export interface LatencyTimeseriesBucket {
  bucket_start: string;
  sample_count: number;
  e2e_p50_ms: number | null;
  e2e_p95_ms: number | null;
  provider_p50_ms: number | null;
  provider_p95_ms: number | null;
  overhead_p50_ms: number | null;
  overhead_p95_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
}

/** Latency percentiles bucketed over a window, for charting. */
export interface LatencyTimeseriesResponse {
  start: string;
  end: string;
  interval: "minute" | "hour" | "day";
  buckets: LatencyTimeseriesBucket[];
}
