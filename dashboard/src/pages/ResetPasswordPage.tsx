import { useState } from "react";
import { resetPassword } from "../api/auth";

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
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  /**
   * Submits the reset-password form: reads the `token` from the URL and
   * calls `resetPassword()` with it and the new password, marking the
   * page as submitted on success.
   *
   * @param e - The form submit event.
   */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
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
        <label htmlFor="reset-new-password" className="mb-1 block text-sm text-slate-300">
          New password
        </label>
        <input
          id="reset-new-password"
          type="password"
          autoFocus
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy}
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
