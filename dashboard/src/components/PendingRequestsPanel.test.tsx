import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PendingRequestsPanel from "./PendingRequestsPanel";

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

it("lists pending accounts and approves one", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts?: RequestInit) => {
    calls.push(`${opts?.method ?? "GET"} ${url}`);
    if (url.endsWith("/accounts/pending")) {
      return { ok: true, status: 200, json: async () => ({
        accounts: [{ account_id: 7, name: "new@x.com", email: "new@x.com",
                     created_at: "2026-08-26T00:00:00Z" }] }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({
      id: 7, name: "new@x.com", status: "approved" }) } as Response;
  }));
  render(<PendingRequestsPanel />);
  await waitFor(() => expect(screen.getByText("new@x.com")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: /approve/i }));
  await waitFor(() => expect(calls.some((c) => c.includes("/approve"))).toBe(true));
});

it("approves with an unlimited (null) budget when the field is left empty", async () => {
  const bodies: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts?: RequestInit) => {
    if (url.endsWith("/accounts/pending")) {
      return { ok: true, status: 200, json: async () => ({
        accounts: [{ account_id: 8, name: "empty@x.com", email: "empty@x.com",
                     created_at: "2026-08-26T00:00:00Z" }] }) } as Response;
    }
    if (opts?.body) bodies.push(JSON.parse(opts.body as string));
    return { ok: true, status: 200, json: async () => ({
      id: 8, name: "empty@x.com", status: "approved" }) } as Response;
  }));
  render(<PendingRequestsPanel />);
  await waitFor(() => expect(screen.getByText("empty@x.com")).toBeTruthy());
  // Budget input left blank (default state) - approve should still succeed
  // and send an explicit `null` (unlimited), not silently reject.
  fireEvent.click(screen.getByRole("button", { name: /approve/i }));
  await waitFor(() => expect(bodies.length).toBe(1));
  expect(bodies[0]).toMatchObject({ monthly_budget_usd: null });
});

it("blocks approval with a validation message when the budget is non-numeric junk", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts?: RequestInit) => {
    calls.push(`${opts?.method ?? "GET"} ${url}`);
    if (url.endsWith("/accounts/pending")) {
      return { ok: true, status: 200, json: async () => ({
        accounts: [{ account_id: 9, name: "junk@x.com", email: "junk@x.com",
                     created_at: "2026-08-26T00:00:00Z" }] }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({
      id: 9, name: "junk@x.com", status: "approved" }) } as Response;
  }));
  render(<PendingRequestsPanel />);
  await waitFor(() => expect(screen.getByText("junk@x.com")).toBeTruthy());
  const input = screen.getByPlaceholderText("unlimited");
  // A native <input type="number"> sanitizes non-numeric keystrokes to ""
  // before React ever sees them, so drop to type="text" to simulate a
  // browser/input-method edge case that still hands the handler a
  // non-empty, non-numeric string - the case the guard exists for.
  input.setAttribute("type", "text");
  fireEvent.change(input, { target: { value: "abc" } });
  fireEvent.click(screen.getByRole("button", { name: /approve/i }));
  await waitFor(() => expect(screen.getByText(/must be a number/i)).toBeTruthy());
  // Must never silently fall through to an "unlimited" approve.
  expect(calls.some((c) => c.includes("/approve"))).toBe(false);
});

it("does not fire a second /approve request when the button is clicked again before the first request resolves", async () => {
  const approveCalls: string[] = [];
  let resolveApprove: (() => void) | undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts?: RequestInit) => {
    if (url.endsWith("/accounts/pending")) {
      return { ok: true, status: 200, json: async () => ({
        accounts: [{ account_id: 10, name: "slow@x.com", email: "slow@x.com",
                     created_at: "2026-08-26T00:00:00Z" }] }) } as Response;
    }
    if (url.includes("/approve")) {
      approveCalls.push(`${opts?.method ?? "GET"} ${url}`);
      // Simulate real network latency: the request does not resolve
      // immediately, mirroring the window during which an end user
      // might spam-click "Approve" thinking nothing happened.
      await new Promise<void>((resolve) => { resolveApprove = resolve; });
      return { ok: true, status: 200, json: async () => ({
        id: 10, name: "slow@x.com", status: "approved" }) } as Response;
    }
    return { ok: true, status: 200, json: async () => ({
      id: 10, name: "slow@x.com", status: "approved" }) } as Response;
  }));
  render(<PendingRequestsPanel />);
  await waitFor(() => expect(screen.getByText("slow@x.com")).toBeTruthy());
  const button = screen.getByRole("button", { name: /approve/i });
  fireEvent.click(button);
  await waitFor(() => expect(approveCalls.length).toBe(1));
  // Spam-click while the first request is still in flight, as an
  // impatient end user (seeing no visible feedback) would.
  fireEvent.click(button);
  fireEvent.click(button);
  fireEvent.click(button);
  resolveApprove?.();
  await waitFor(() => expect(approveCalls.length).toBeGreaterThanOrEqual(1));
  expect(approveCalls.length).toBe(1);
});
