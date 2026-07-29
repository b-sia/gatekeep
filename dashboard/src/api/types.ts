export interface UsageBreakdownRow {
  key: string;
  label?: string | null;
  request_count: number;
  total_tokens: number;
  cost_usd: number;
  cache_hit_count: number;
}

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

export interface TimeseriesBucket {
  bucket_start: string;
  request_count: number;
  cache_hit_count: number;
  cost_usd: number;
}

export interface TimeseriesResponse {
  start: string;
  end: string;
  interval: string;
  buckets: TimeseriesBucket[];
}

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

export interface EvalHistoryResponse {
  runs: EvalRunOut[];
}

export interface PromptOut {
  name: string;
  active_version_num: number | null;
  created_at: string;
  updated_at: string;
}

export interface PromptListResponse {
  prompts: PromptOut[];
}

export interface PromptVersionOut {
  version_num: number;
  active: boolean;
  created_at: string;
  created_by: string | null;
  notes: string | null;
}

export interface PromptVersionTimelineResponse {
  name: string;
  versions: PromptVersionOut[];
}
