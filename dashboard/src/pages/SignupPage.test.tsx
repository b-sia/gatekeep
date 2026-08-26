import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SignupPage from "./SignupPage";

afterEach(() => vi.unstubAllGlobals());

it("shows a check-your-email message after signup", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 202, json: async () => ({ status: "ok" }) } as Response)));
  render(<SignupPage onBackToLogin={() => {}} />);
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "e@x.com" } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "pw123456" } });
  fireEvent.click(screen.getByRole("button", { name: /sign up/i }));
  await waitFor(() => expect(screen.getByText(/check your email/i)).toBeTruthy());
});
