import { useState } from "react";
import { resetPassword } from "../api/auth";
import PasswordInput from "../components/PasswordInput";

interface ResetPasswordPageProps {
  /** Called when the user wants to go to the login page. */
  onGoToLogin?: () => void;
}

/**
 * Reset-password page: reads the `token` query parameter from the URL,
 * takes a new password, and submits to `resetPassword()`. Shows a
 * success message with a link to sign in once complete.
 *
 * @param props - See {@link ResetPasswordPageProps}.
 */
export default function ResetPasswordPage({ onGoToLogin }: ResetPasswordPageProps) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const passwordsMismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;
  const canSubmit = newPassword.length > 0 && confirmPassword.length > 0 && newPassword === confirmPassword;

  /**
   * Submits the reset-password form: reads the `token` from the URL and
   * calls `resetPassword()` with it and the new password, marking the
   * page as submitted on success.
   *
   * @param e - The form submit event.
   */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) {
      setError("Missing reset token.");
      return;
    }
    setBusy(true);
    try {
      await resetPassword(token, newPassword);
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reset password");
    } finally {
      setBusy(false);
    }
  }

  if (submitted) {
    return (
      <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
        <h1 className="mb-4 text-lg font-semibold text-slate-100">Password reset</h1>
        <p className="text-sm text-slate-300">Your password has been reset.</p>
        <button
          type="button"
          onClick={onGoToLogin}
          className="mt-4 text-sm text-indigo-400 hover:text-indigo-300"
        >
          Sign in
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
      <h1 className="mb-4 text-lg font-semibold text-slate-100">Reset password</h1>
      <form onSubmit={handleSubmit}>
        <PasswordInput
          id="reset-new-password"
          label="New password"
          value={newPassword}
          onChange={setNewPassword}
          autoFocus
        />
        <PasswordInput
          id="reset-confirm-password"
          label="Confirm password"
          value={confirmPassword}
          onChange={setConfirmPassword}
        />
        {passwordsMismatch && (
          <p className="mb-3 text-sm text-red-400">Passwords do not match</p>
        )}
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy || !canSubmit}
          className="w-full rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Reset password
        </button>
      </form>
      <button
        type="button"
        onClick={onGoToLogin}
        className="mt-4 text-sm text-slate-400 hover:text-slate-200"
      >
        Back to sign in
      </button>
    </div>
  );
}
