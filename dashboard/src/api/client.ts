import type {
  EvalHistoryResponse,
  PromptListResponse,
  PromptVersionTimelineResponse,
  TimeseriesResponse,
  UsageSummaryResponse,
} from "./types";

const STORAGE_KEY = "gatekeep_dashboard_api_key";

export function getStoredApiKey(): string | null {
  return localStorage.getItem(STORAGE_KEY);
}

export function setStoredApiKey(key: string): void {
  localStorage.setItem(STORAGE_KEY, key);
}

export function clearStoredApiKey(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export class UnauthorizedError extends Error {}

async function request<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const apiKey = getStoredApiKey();
  if (!apiKey) {
    throw new UnauthorizedError("No API key stored");
  }
  const url = new URL(`/dashboard/api/${path}`, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (response.status === 401) {
    clearStoredApiKey();
    throw new UnauthorizedError("API key was rejected");
  }
  if (!response.ok) {
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export interface UsageFilters {
  start?: string;
  end?: string;
  model?: string;
  keyId?: number;
  promptName?: string;
}

export function getUsageSummary(filters: UsageFilters): Promise<UsageSummaryResponse> {
  return request<UsageSummaryResponse>("usage/summary", {
    start: filters.start,
    end: filters.end,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

export function getUsageTimeseries(
  filters: UsageFilters & { interval: "hour" | "day" },
): Promise<TimeseriesResponse> {
  return request<TimeseriesResponse>("usage/timeseries", {
    start: filters.start,
    end: filters.end,
    interval: filters.interval,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

export function getEvalHistory(promptName?: string): Promise<EvalHistoryResponse> {
  return request<EvalHistoryResponse>("evals", { prompt_name: promptName });
}

export function getPrompts(): Promise<PromptListResponse> {
  return request<PromptListResponse>("prompts");
}

export function getPromptVersions(name: string): Promise<PromptVersionTimelineResponse> {
  return request<PromptVersionTimelineResponse>(`prompts/${encodeURIComponent(name)}/versions`);
}
