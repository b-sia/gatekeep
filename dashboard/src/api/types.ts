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
  cache_hit_count: number;
  cache_hit_rate: number;
  by_model: UsageBreakdownRow[];
  by_key: UsageBreakdownRow[];
  by_prompt: UsageBreakdownRow[];
}

/** One bucket of a usage timeseries (requests/cache hits/cost within a
 * single hour or day interval). */
export interface TimeseriesBucket {
  bucket_start: string;
  request_count: number;
  cache_hit_count: number;
  cost_usd: number;
}

/** Usage broken into fixed-size time buckets over a window, for charting. */
export interface TimeseriesResponse {
  start: string;
  end: string;
  interval: string;
  buckets: TimeseriesBucket[];
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
