import type {
  EvalHistoryResponse,
  PromptListResponse,
  PromptVersionTimelineResponse,
  TimeseriesResponse,
  UsageByModelTimeseriesResponse,
  UsageSummaryResponse,
} from "./types";

const STORAGE_KEY = "gatekeep_dashboard_api_key";

/** Reads the dashboard API key from localStorage, if one has been saved. */
export function getStoredApiKey(): string | null {
  return localStorage.getItem(STORAGE_KEY);
}

/** Persists the dashboard API key to localStorage. */
export function setStoredApiKey(key: string): void {
  localStorage.setItem(STORAGE_KEY, key);
}

/** Removes the stored dashboard API key, e.g. after it's rejected or the
 * user chooses to replace it. */
export function clearStoredApiKey(): void {
  localStorage.removeItem(STORAGE_KEY);
}

/** Thrown when a dashboard API request has no stored key, or the gateway
 * rejects the stored key with a 401 (in which case the key is also cleared
 * from storage as a side effect). */
export class UnauthorizedError extends Error {}

/**
 * Issues an authenticated GET request against `/dashboard/api/<path>`.
 *
 * @param path - API path under `/dashboard/api/`, without a leading slash.
 * @param params - Query params to attach; entries with an `undefined` value
 *   are omitted.
 * @returns The parsed JSON response body.
 * @throws {UnauthorizedError} If no API key is stored, or the gateway
 *   responds 401 (the stored key is cleared in that case).
 * @throws {Error} For any other non-OK response status.
 */
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

/** Common filters accepted by the usage summary/timeseries endpoints. All
 * fields are optional; omitting one leaves that dimension unfiltered. */
export interface UsageFilters {
  start?: string;
  end?: string;
  model?: string;
  keyId?: number;
  promptName?: string;
}

/** Fetches aggregate usage/cost totals and breakdowns for the given filters. */
export function getUsageSummary(filters: UsageFilters): Promise<UsageSummaryResponse> {
  return request<UsageSummaryResponse>("usage/summary", {
    start: filters.start,
    end: filters.end,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

/** Fetches usage bucketed into minute, hourly, or daily intervals for the
 * given filters, for charting over time. */
export function getUsageTimeseries(
  filters: UsageFilters & { interval: "minute" | "hour" | "day" },
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

/** Fetches usage bucketed by both time and model, for the per-model usage
 * panel. */
export function getUsageTimeseriesByModel(
  filters: UsageFilters & { interval: "minute" | "hour" | "day" },
): Promise<UsageByModelTimeseriesResponse> {
  return request<UsageByModelTimeseriesResponse>("usage/timeseries/by-model", {
    start: filters.start,
    end: filters.end,
    interval: filters.interval,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

/** Fetches eval run history, optionally scoped to a single prompt name. */
export function getEvalHistory(promptName?: string): Promise<EvalHistoryResponse> {
  return request<EvalHistoryResponse>("evals", { prompt_name: promptName });
}

/** Fetches all registered prompts. */
export function getPrompts(): Promise<PromptListResponse> {
  return request<PromptListResponse>("prompts");
}

/** Fetches the version history for a single named prompt. */
export function getPromptVersions(name: string): Promise<PromptVersionTimelineResponse> {
  return request<PromptVersionTimelineResponse>(`prompts/${encodeURIComponent(name)}/versions`);
}
