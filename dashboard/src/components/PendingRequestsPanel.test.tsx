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
