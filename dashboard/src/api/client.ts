import type {
  AccountCreateRequest,
  AccountListResponse,
  AccountOut,
  AccountPatchRequest,
  AuditFeedResponse,
  CandidateResponse,
  CurationResponse,
  EvalCaseOut,
  EvalHistoryResponse,
  JobCreatedResponse,
  JobStatusResponse,
  KeyCreatedResponse,
  KeyListResponse,
  KeyOut,
  LatencySummaryResponse,
  LatencyTimeseriesResponse,
  MeResponse,
  PendingAccountsResponse,
  PromptListResponse,
  PromptMutationResponse,
  PromptSuiteResponse,
  PromptTrafficResponse,
  PromptVersionTimelineResponse,
  SuiteOut,
  TimeseriesResponse,
  UsageByModelTimeseriesResponse,
  UsageSummaryResponse,
} from "./types";
/** Thrown when a dashboard API request gets a 401 - the caller has no valid
 * session cookie (never logged in, logged out, or the session expired). */
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
 * Reads the `gk_csrf` cookie set by the server after login, for attaching to
 * non-GET requests as `X-CSRF-Token`.
 *
 * @returns The cookie's value, or an empty string if it is not set.
 */
function readCsrfCookie(): string {
  const match = document.cookie.match(/(?:^|;\s*)gk_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

/**
 * Throws `UnauthorizedError`. Called from `request`/`mutate` when the
 * gateway returns 401, meaning the caller's session cookie is missing or
 * invalid.
 *
 * @throws {UnauthorizedError} Always.
 */
function handleRejectedSession(): never {
  throw new UnauthorizedError("Not authenticated");
}

/**
 * Issues an authenticated GET request against `/dashboard/api/<path>`.
 *
 * @param path - API path under `/dashboard/api/`, without a leading slash.
 * @param params - Query params to attach; entries with an `undefined` value
 *   are omitted.
 * @returns The parsed JSON response body.
 * @throws {UnauthorizedError} If the gateway responds 401 (no valid session).
 * @throws {Error} For any other non-OK response; the thrown message includes
 *   the server's error message when the body is OpenAI-shaped.
 */
async function request<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const url = new URL(`/dashboard/api/${path}`, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url.toString(), {
    credentials: "include",
  });
  if (response.status === 401) {
    handleRejectedSession();
  }
  if (!response.ok) {
    throw new Error(await errorMessage(response, path));
  }
  return response.json() as Promise<T>;
}

/**
 * Issues an authenticated POST/PATCH against `/dashboard/api/<path>` with a
 * JSON body, mirroring `request`'s cookie-auth and 401 handling.
 *
 * @param method - "POST", "PATCH", "PUT", or "DELETE".
 * @param path - API path under `/dashboard/api/`, without a leading slash.
 * @param body - JSON-serializable request body, or undefined for none.
 * @returns The parsed JSON response body.
 * @throws {UnauthorizedError} If the gateway responds 401 (no valid session).
 * @throws {Error} For any other non-OK response; the thrown message includes
 *   the server's error message when the body is OpenAI-shaped.
 */
async function mutate<T>(
  method: "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const url = new URL(`/dashboard/api/${path}`, window.location.origin);
  const response = await fetch(url.toString(), {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readCsrfCookie(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    handleRejectedSession();
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

/** Lists self-serve signup requests awaiting approval (operator only). */
export function getPending(): Promise<PendingAccountsResponse> {
  return request<PendingAccountsResponse>("accounts/pending");
}

/**
 * Approves a pending signup request, creating its account (operator only).
 *
 * @param accountId - The pending account's id.
 * @param monthlyBudgetUsd - Monthly budget cap to assign, or null for
 *   unlimited.
 */
export function approveAccount(
  accountId: number,
  monthlyBudgetUsd: number | null,
): Promise<AccountOut> {
  return mutate<AccountOut>("POST", `accounts/${accountId}/approve`, {
    monthly_budget_usd: monthlyBudgetUsd,
  });
}

/** Rejects a pending signup request (operator only). */
export function rejectAccount(accountId: number): Promise<{ status: string }> {
  return mutate<{ status: string }>("POST", `accounts/${accountId}/reject`);
}

/** Fetches a prompt's eval suite and cases (null suite => none registered). */
export function getPromptSuite(name: string): Promise<PromptSuiteResponse> {
  return request<PromptSuiteResponse>(`prompts/${encodeURIComponent(name)}/suite`);
}

/** Fetches a prompt's unreviewed curated cases. */
export function getPromptCuration(name: string): Promise<CurationResponse> {
  return request<CurationResponse>(`prompts/${encodeURIComponent(name)}/curation`);
}

/** Fetches the audit feed, filterable by entity/action. */
export function getAuditFeed(params?: {
  entityType?: string;
  entityRef?: string;
  action?: string;
  limit?: number;
}): Promise<AuditFeedResponse> {
  return request<AuditFeedResponse>("audit", {
    entity_type: params?.entityType,
    entity_ref: params?.entityRef,
    action: params?.action,
    limit: params?.limit,
  });
}

/** Polls one background job's status. */
export function getJob(jobId: string): Promise<JobStatusResponse> {
  return request<JobStatusResponse>(`prompts/jobs/${encodeURIComponent(jobId)}`);
}

/** Creates a prompt (initial active version 1). */
export function createPrompt(body: {
  name: string;
  template: string;
  notes?: string;
}): Promise<PromptMutationResponse> {
  return mutate<PromptMutationResponse>("POST", "prompts", body);
}

/** Appends a new inactive version to a prompt. */
export function addPromptVersion(
  name: string,
  body: { template: string; notes?: string },
): Promise<PromptMutationResponse> {
  return mutate<PromptMutationResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/versions`,
    body,
  );
}

/** Kicks off an eval-gated promotion; returns a job id to poll. */
export function promotePrompt(
  name: string,
  versionNum: number,
): Promise<JobCreatedResponse> {
  return mutate<JobCreatedResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/promote`,
    { version_num: versionNum },
  );
}

/** Rolls a prompt back to its previously-active version. */
export function rollbackPrompt(name: string): Promise<PromptMutationResponse> {
  return mutate<PromptMutationResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/rollback`,
  );
}

/** Sets/adjusts a prompt's A/B candidate version + traffic split. */
export function setCandidate(
  name: string,
  body: { version_num: number; traffic_pct: number },
): Promise<CandidateResponse> {
  return mutate<CandidateResponse>(
    "PUT",
    `prompts/${encodeURIComponent(name)}/candidate`,
    body,
  );
}

/** Clears a prompt's A/B candidate (100% back to active). */
export function clearCandidate(name: string): Promise<CandidateResponse> {
  return mutate<CandidateResponse>(
    "DELETE",
    `prompts/${encodeURIComponent(name)}/candidate`,
  );
}

/** Fetches actual per-version request counts for a prompt (trailing 7 days
 * when start/end are omitted), to compare against the configured candidate split. */
export function getPromptTraffic(
  name: string,
  filters?: { start?: string; end?: string },
): Promise<PromptTrafficResponse> {
  return request<PromptTrafficResponse>(`prompts/${encodeURIComponent(name)}/traffic`, {
    start: filters?.start,
    end: filters?.end,
  });
}

/** Creates an eval suite for a prompt (threshold defaults server-side). */
export function createSuite(
  name: string,
  body: { threshold?: number },
): Promise<SuiteOut> {
  return mutate<SuiteOut>(
    "POST",
    `prompts/${encodeURIComponent(name)}/suite`,
    body,
  );
}

/** Adds a reviewed manual eval case to a prompt's suite. */
export function addCase(
  name: string,
  body: {
    input_messages: Array<Record<string, unknown>>;
    check_type: string;
    expected?: string;
    judge_criteria?: string;
  },
): Promise<EvalCaseOut> {
  return mutate<EvalCaseOut>(
    "POST",
    `prompts/${encodeURIComponent(name)}/suite/cases`,
    body,
  );
}

/** Kicks off an on-demand eval run; returns a job id to poll. */
export function runEval(
  name: string,
  body: { version_num?: number; model?: string },
): Promise<JobCreatedResponse> {
  return mutate<JobCreatedResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/eval-run`,
    body,
  );
}

/** Mines recent samples into unreviewed curated cases. */
export function mineCuration(
  name: string,
  body: { limit?: number },
): Promise<CurationResponse> {
  return mutate<CurationResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/curation/mine`,
    body,
  );
}

/** Approves or rejects one curated case. */
export function reviewCase(
  name: string,
  caseId: number,
  approved: boolean,
): Promise<{ status: string }> {
  return mutate<{ status: string }>(
    "POST",
    `prompts/${encodeURIComponent(name)}/curation/${caseId}/review`,
    { approved },
  );
}
