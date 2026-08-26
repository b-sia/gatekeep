import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import App from "./App";

afterEach(() => vi.unstubAllGlobals());

/**
 * Builds a minimal stand-in for a fetch `Response`.
 *
 * @param body - Value resolved by the stand-in's `json()` method.
 * @param status - HTTP status code to report (default 200).
 * @returns An object usable wherever `App`/`client.ts` expects a `Response`.
 */
function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

it("shows login when unauthenticated (401 from /me)", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({}, 401)));
  render(<App />);
  // LoginPage renders "Sign in" both as its heading and its submit button,
  // so match on the heading specifically rather than getByText (which would
  // ambiguously match both).
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeTruthy(),
  );
});

it("shows pending page when account status is pending", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      jsonResponse({ account_id: 1, status: "pending", is_operator: false }),
    ),
  );
  render(<App />);
  await waitFor(() => expect(screen.getByText(/awaiting approval/i)).toBeTruthy());
});
