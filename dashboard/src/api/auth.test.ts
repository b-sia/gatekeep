import { afterEach, describe, expect, it, vi } from "vitest";
import { login } from "./auth";

afterEach(() => vi.unstubAllGlobals());

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("auth api", () => {
  it("login posts credentials with cookies and returns status", async () => {
    const fetchMock = vi.fn(async (..._args: unknown[]) =>
      jsonResponse({ account_id: 1, status: "pending", is_operator: false, csrf_token: "c" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const res = await login("e@x.com", "pw123456");
    expect(res.status).toBe("pending");
    const opts = fetchMock.mock.calls[0][1] as RequestInit;
    expect(opts.credentials).toBe("include");
    expect(opts.method).toBe("POST");
  });
});
