import type {
  AccountCreateRequest,
  AccountListResponse,
  AccountOut,
  AccountPatchRequest,
  EvalHistoryResponse,
  KeyCreatedResponse,
  KeyListResponse,
  KeyOut,
  LatencySummaryResponse,
  LatencyTimeseriesResponse,
  MeResponse,
  PromptListResponse,
  PromptVersionTimelineResponse,
  TimeseriesResponse,
  UsageByModelTimeseriesResponse,
  UsageSummaryResponse,
} from "./types";
import { getActiveIdentity, getActiveKey, markInvalid } from "./identityStore";

/** Thrown when a dashboard API request has no active identity, or the
 * gateway rejects the active key with a 401 (in which case that identity is
 * marked invalid in the roster as a side effect). */
export class UnauthorizedError extends Error {}

/**
 * Builds the error message for a non-OK response, preferring the server's
 * OpenAI-shaped error message over a generic status-code message.
 *
 * @param response - The non-OK fetch response.
 * @param path - API path the request was made to, for the fallback message.
 * @returns The server's error message, or a generic fallback if the body
 *   isn't JSON or doesn't carry an `error.message`.
 */
async function errorMessage(response: Response, path: string): Promise<string> {
  let message = `Request to ${path} failed with status ${response.status}`;
  try {
    const payload = await response.json();
    if (payload?.error?.message) message = payload.error.message;
  } catch {
    // Non-JSON error body; keep the generic message.
  }
  return message;
}

/**
 * Marks this tab's active identity invalid (if one is set) and throws.
 * Called from `request`/`mutate` when the gateway returns 401 so the roster
 * reflects the dead key and the tab drops back to the picker.
 *
 * @throws {UnauthorizedError} Always.
 */
function handleRejectedKey(): never {
  const active = getActiveIdentity();
  if (active) markInvalid(active.id);
  throw new UnauthorizedError("API key was rejected");
}

/**
 * Validates a raw Gatekeep key by calling `GET /me` with it directly,
 * without touching the roster or the active pointer. Used by the identity
 * picker before a key is saved.
 *
 * @param key - The raw key to validate.
 * @returns The caller's account context if the key is accepted.
 * @throws {UnauthorizedError} If the gateway rejects the key with a 401.
 * @throws {Error} For any other non-OK response.
 */
export async function validateKey(key: string): Promise<MeResponse> {
  const url = new URL("/dashboard/api/me", window.location.origin);
  const response = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${key}` },
  });
  if (response.status === 401) {
    throw new UnauthorizedError("API key was rejected");
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response, "me"));
  }
  return response.json() as Promise<MeResponse>;
}

/**
 * Issues an authenticated GET request against `/dashboard/api/<path>`.
 *
 * @param path - API path under `/dashboard/api/`, without a leading slash.
 * @param params - Query params to attach; entries with an `undefined` value
 *   are omitted.
 * @returns The parsed JSON response body.
 * @throws {UnauthorizedError} If no active identity is set, or the gateway
 *   responds 401 (that identity is marked invalid).
 * @throws {Error} For any other non-OK response; the thrown message includes
 *   the server's error message when the body is OpenAI-shaped.
 */
async function request<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const apiKey = getActiveKey();
  if (!apiKey) {
    throw new UnauthorizedError("No active identity");
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
    handleRejectedKey();
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response, path));
  }
  return response.json() as Promise<T>;
}

/**
 * Issues an authenticated POST/PATCH against `/dashboard/api/<path>` with a
 * JSON body, mirroring `request`'s bearer-auth and 401 handling.
 *
 * @param method - "POST" or "PATCH".
 * @param path - API path under `/dashboard/api/`, without a leading slash.
 * @param body - JSON-serializable request body, or undefined for none.
 * @returns The parsed JSON response body.
 * @throws {UnauthorizedError} If no active identity is set, or the gateway
 *   responds 401 (that identity is marked invalid).
 * @throws {Error} For any other non-OK response; the thrown message includes
 *   the server's error message when the body is OpenAI-shaped.
 */
async function mutate<T>(
  method: "POST" | "PATCH",
  path: string,
  body?: unknown,
): Promise<T> {
  const apiKey = getActiveKey();
  if (!apiKey) {
    throw new UnauthorizedError("No active identity");
  }
  const url = new URL(`/dashboard/api/${path}`, window.location.origin);
  const response = await fetch(url.toString(), {
    method,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    handleRejectedKey();
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response, path));
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

/** Fetches latency percentiles and breakdowns for the given filters. */
export function getLatencySummary(
  filters: UsageFilters,
): Promise<LatencySummaryResponse> {
  return request<LatencySummaryResponse>("latency/summary", {
    start: filters.start,
    end: filters.end,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

/** Fetches latency percentiles bucketed into minute, hourly, or daily
 * intervals, for charting over time. */
export function getLatencyTimeseries(
  filters: UsageFilters & { interval: "minute" | "hour" | "day" },
): Promise<LatencyTimeseriesResponse> {
  return request<LatencyTimeseriesResponse>("latency/timeseries", {
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

/** Fetches the caller's own account context (id, name, operator flag,
 * budget, spend). */
export function getMe(): Promise<MeResponse> {
  return request<MeResponse>("me");
}

/** Lists an account's keys (active and revoked). */
export function getAccountKeys(accountId: number): Promise<KeyListResponse> {
  return request<KeyListResponse>(`accounts/${accountId}/keys`);
}

/** Mints a key for an account; the response carries the raw key once. */
export function createKey(accountId: number, name: string): Promise<KeyCreatedResponse> {
  return mutate<KeyCreatedResponse>("POST", `accounts/${accountId}/keys`, { name });
}

/** Soft-revokes a key on an account. */
export function revokeKey(accountId: number, keyId: number): Promise<KeyOut> {
  return mutate<KeyOut>("POST", `accounts/${accountId}/keys/${keyId}/revoke`);
}

/** Lists all accounts with stats (operator only). */
export function getAccounts(): Promise<AccountListResponse> {
  return request<AccountListResponse>("accounts");
}

/** Creates an account (operator only). */
export function createAccount(body: AccountCreateRequest): Promise<AccountOut> {
  return mutate<AccountOut>("POST", "accounts", body);
}

/** Updates an account (operator only). */
export function patchAccount(
  accountId: number,
  body: AccountPatchRequest,
): Promise<AccountOut> {
  return mutate<AccountOut>("PATCH", `accounts/${accountId}`, body);
}
