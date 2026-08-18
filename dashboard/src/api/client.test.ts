import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UnauthorizedError, getMe, validateKey } from "./client";
import { addIdentity, getActiveIdentity, listIdentities, setActiveIdentity } from "./identityStore";
import type { MeResponse } from "./types";

/** Fresh storage before every test so cases do not leak roster/pointer state
 * into one another, and a clean global-stub slate after every test so a
 * mocked `fetch` from one test never leaks into the next. */
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

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
};

describe("request <-> identityStore contract", () => {
  it("throws UnauthorizedError without calling fetch when no active identity is set", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMe()).rejects.toThrow(UnauthorizedError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("marks the active identity invalid in the roster on a 401", async () => {
    const created = addIdentity({
      key: "sk-a",
      accountId: 1,
      accountName: "Alice",
      isOperator: false,
    });
    setActiveIdentity(created.id);
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, {}));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMe()).rejects.toThrow(UnauthorizedError);

    const roster = listIdentities();
    expect(roster.find((entry) => entry.id === created.id)?.status).toBe("invalid");
  });
});

describe("validateKey", () => {
  it("sends the given raw key as the bearer token and leaves storage untouched on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(200, ME));
    vi.stubGlobal("fetch", fetchMock);

    const rosterBefore = listIdentities();
    const activeBefore = getActiveIdentity();

    const result = await validateKey("sk-standalone");

    expect(result).toEqual(ME);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, options] = fetchMock.mock.calls[0];
    expect((options?.headers as Record<string, string>).Authorization).toBe(
      "Bearer sk-standalone",
    );
    expect(listIdentities()).toEqual(rosterBefore);
    expect(getActiveIdentity()).toEqual(activeBefore);
  });

  it("throws UnauthorizedError on a 401 and still does not touch the roster or active pointer", async () => {
    const fetchMock = vi.fn().mockResolvedValue(fakeResponse(401, {}));
    vi.stubGlobal("fetch", fetchMock);

    const rosterBefore = listIdentities();
    const activeBefore = getActiveIdentity();

    await expect(validateKey("sk-bad")).rejects.toThrow(UnauthorizedError);

    expect(listIdentities()).toEqual(rosterBefore);
    expect(getActiveIdentity()).toEqual(activeBefore);
  });
});
