import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ResetPasswordPage from "./ResetPasswordPage";

beforeEach(() => {
  window.history.pushState({}, "", "/dashboard/reset-password?token=tok123");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("resets the password and shows a success message", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ status: "ok" }) }) as Response),
  );
  render(<ResetPasswordPage onGoToLogin={() => {}} />);
  fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "pw123456" } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "pw123456" } });
  fireEvent.click(screen.getByRole("button", { name: /reset password/i }));
  await waitFor(() => expect(screen.getByText(/password has been reset/i)).toBeTruthy());
});

it("disables submit and shows an error when passwords do not match", () => {
  render(<ResetPasswordPage onGoToLogin={() => {}} />);
  fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "pw123456" } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "different" } });
  expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: /reset password/i }) as HTMLButtonElement).disabled,
  ).toBe(true);
});
