import { useState } from "react";
import { signup } from "../api/auth";

interface SignupPageProps {
  /** Called when the user wants to go back to the login page. */
  onBackToLogin: () => void;
}

/**
 * Signup form: email + password, submits to `signup()`. On success the
 * form is replaced with a "check your email" confirmation message.
 *
 * @param props - See {@link SignupPageProps}.
 */
export default function SignupPage({ onBackToLogin }: SignupPageProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  /**
   * Submits the signup form: calls `signup()` with the current email and
   * password, then marks the form as submitted on success so the
   * confirmation message is shown.
   *
   * @param e - The form submit event.
   */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await signup(email, password);
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to sign up");
    } finally {
      setBusy(false);
    }
  }

  if (submitted) {
    return (
      <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
        <h1 className="mb-4 text-lg font-semibold text-slate-100">Almost there</h1>
        <p className="text-sm text-slate-300">Check your email to verify your account.</p>
        <button
          type="button"
          onClick={onBackToLogin}
          className="mt-4 text-sm text-indigo-400 hover:text-indigo-300"
        >
          Back to sign in
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
      <h1 className="mb-4 text-lg font-semibold text-slate-100">Sign up</h1>
      <form onSubmit={handleSubmit}>
        <label htmlFor="signup-email" className="mb-1 block text-sm text-slate-300">
          Email
        </label>
        <input
          id="signup-email"
          type="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <label htmlFor="signup-password" className="mb-1 block text-sm text-slate-300">
          Password
        </label>
        <input
          id="signup-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-indigo-600 px-3 py-2 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Sign up
        </button>
      </form>
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
