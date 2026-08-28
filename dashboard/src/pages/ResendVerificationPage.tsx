import { useState } from "react";
import { resendVerification } from "../api/auth";

interface ResendVerificationPageProps {
  /** Called when the user wants to go back to the login page. */
  onBackToLogin?: () => void;
}

/**
 * Resend-verification form: email input, submits to `resendVerification()`.
 * Always shows the same neutral confirmation message regardless of whether
 * the email exists or is already verified, to avoid account enumeration.
 *
 * @param props - See {@link ResendVerificationPageProps}.
 */
export default function ResendVerificationPage({ onBackToLogin }: ResendVerificationPageProps) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  /**
   * Submits the resend-verification form: calls `resendVerification()` with
   * the current email. Always shows the neutral confirmation afterward, even
   * if the request fails, so the response can't be used to enumerate which
   * emails have accounts or are already verified.
   *
   * @param e - The form submit event.
   */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await resendVerification(email);
    } catch {
      // Deliberately ignored: always show the same neutral confirmation.
    } finally {
      setBusy(false);
      setSubmitted(true);
    }
  }

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
      <h1 className="mb-4 text-lg font-semibold text-slate-100">Resend verification email</h1>
      {submitted ? (
        <p className="text-sm text-slate-300">
          If that email has a pending, unverified account, a new verification link is on its way.
        </p>
      ) : (
        <form onSubmit={handleSubmit}>
          <label htmlFor="resend-email" className="mb-1 block text-sm text-slate-300">
            Email
          </label>
          <input
            id="resend-email"
            type="email"
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
          />
          <button
            type="submit"
            disabled={busy}
            className="w-full rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Resend verification email
          </button>
        </form>
      )}
      <button
        type="button"
        onClick={onBackToLogin}
        className="mt-4 text-sm text-slate-400 hover:text-slate-200"
      >
        Back to sign in
      </button>
    </div>
  );
}
