import { afterEach, describe, expect, it, vi } from "vitest";
import {
  UnauthorizedError,
  getMe,
  createPrompt,
  promotePrompt,
  setCandidate,
  clearCandidate,
  getJob,
} from "./client";
import type { MeResponse } from "./types";

/** A clean global-stub slate after every test so a mocked `fetch` from one
 * test never leaks into the next. */
afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Builds a minimal stand-in for a fetch `Response`, covering exactly the
 * surface `client.ts` reads (`status`, `ok`, `json()`).
 *
 * @param status - HTTP status code to report.
 * @param body - Value resolved by the stand-in's `json()` method.
 * @returns An object usable wherever `client.ts` expects a `Response`.
 */
function fakeResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

/** A stock `/me` payload for tests that need a valid-looking body. */
const ME: MeResponse = {
  account_id: 7,
  name: "Alice",
  is_operator: false,
  monthly_budget_usd: null,
  spend_mtd: 0,
  status: "approved",
};

describe("getMe", () => {
  it("returns the account context on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, ME));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getMe();

    expect(result).toEqual(ME);
    const [, options] = fetchMock.mock.calls[0];
    expect((options as RequestInit).credentials).toBe("include");
  });

  it("throws UnauthorizedError on a 401", async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, {}));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMe()).rejects.toThrow(UnauthorizedError);
  });
});

describe("prompt-ops client fns", () => {
  it("POSTs a prompt create body", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(fakeResponse(200, { name: "p", version_num: 1 }));
    vi.stubGlobal("fetch", fetchMock);

    const res = await createPrompt({ name: "p", template: "t" });
    expect(res.version_num).toBe(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/dashboard/api/prompts");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ name: "p", template: "t" });
  });

  it("returns a job id from promote", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(fakeResponse(200, { job_id: "abc" }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await promotePrompt("p", 2);
    expect(res.job_id).toBe("abc");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });

  it("PUTs and DELETEs the candidate", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        fakeResponse(200, {
          name: "p",
          candidate_version_num: 2,
          traffic_pct: 10,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    await setCandidate("p", { version_num: 2, traffic_pct: 10 });
    expect(fetchMock.mock.calls[0][1].method).toBe("PUT");
    await clearCandidate("p");
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
  });

  it("GETs a job status", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        fakeResponse(200, {
          id: "j",
          kind: "eval_run",
          prompt_name: "p",
          version_num: null,
          status: "running",
          progress: { done: 1, total: 3 },
          result: null,
          error: null,
          created_at: "",
          updated_at: "",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const res = await getJob("j");
    expect(res.status).toBe("running");
  });
});
