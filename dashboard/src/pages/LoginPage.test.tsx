import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LoginPage from "./LoginPage";

afterEach(() => vi.unstubAllGlobals());

it("submits credentials and calls onLoggedIn with the result", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ account_id: 1, status: "approved", is_operator: false, csrf_token: "c" }),
  } as Response)));
  const onLoggedIn = vi.fn();
  render(<LoginPage onLoggedIn={onLoggedIn} />);
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "e@x.com" } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw123456" } });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
  await waitFor(() => expect(onLoggedIn).toHaveBeenCalledWith(
    expect.objectContaining({ status: "approved" })));
});
