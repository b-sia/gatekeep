import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SignupPage from "./SignupPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows a check-your-email message after signup", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 202, json: async () => ({ status: "ok" }) } as Response)));
  render(<SignupPage onBackToLogin={() => {}} />);
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "e@x.com" } });
  fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "pw123456" } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "pw123456" } });
  fireEvent.click(screen.getByRole("button", { name: /sign up/i }));
  await waitFor(() => expect(screen.getByText(/check your email/i)).toBeTruthy());
});

it("disables submit and shows an error when passwords do not match", () => {
  render(<SignupPage onBackToLogin={() => {}} />);
  fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "pw123456" } });
  fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "different" } });
  expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  expect((screen.getByRole("button", { name: /sign up/i }) as HTMLButtonElement).disabled).toBe(
    true,
  );
});

it("toggles password visibility when Show/Hide is clicked", () => {
  render(<SignupPage onBackToLogin={() => {}} />);
  const passwordInput = screen.getByLabelText(/^password$/i) as HTMLInputElement;
  expect(passwordInput.type).toBe("password");
  fireEvent.click(screen.getAllByRole("button", { name: /show/i })[0]);
  expect(passwordInput.type).toBe("text");
});
