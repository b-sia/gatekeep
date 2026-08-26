import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PendingRequestsPanel from "./PendingRequestsPanel";

afterEach(() => vi.unstubAllGlobals());

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
