import { useState } from "react";
import { login } from "../api/auth";
import type { LoginResult } from "../api/auth";

interface LoginPageProps {
  /** Called with the login result once the credentials are accepted. */
  onLoggedIn: (result: LoginResult) => void;
  /** Called when the user wants to go to the signup page instead. */
  onGoToSignup?: () => void;
  /** Called when the user wants to go to the forgot-password page. */
  onGoToForgotPassword?: () => void;
  /** Called when the user wants to go to the resend-verification page. */
  onGoToResendVerification?: () => void;
}

/**
 * Login form: email + password, submits to `login()` and reports the
 * result back to the caller so the app can route based on account status.
 *
 * @param props - See {@link LoginPageProps}.
 */
export default function LoginPage({
  onLoggedIn,
  onGoToSignup,
  onGoToForgotPassword,
  onGoToResendVerification,
}: LoginPageProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /**
   * Submits the login form: calls `login()` with the current email and
   * password, then forwards the result to `onLoggedIn` on success.
   *
   * @param e - The form submit event.
   */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await login(email, password);
      onLoggedIn(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to sign in");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
      <h1 className="mb-4 text-lg font-semibold text-slate-100">Sign in</h1>
      <form onSubmit={handleSubmit}>
        <label htmlFor="login-email" className="mb-1 block text-sm text-slate-300">
          Email
        </label>
        <input
          id="login-email"
          type="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <label htmlFor="login-password" className="mb-1 block text-sm text-slate-300">
          Password
        </label>
        <input
          id="login-password"
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
          Sign in
        </button>
      </form>
      <div className="mt-4 flex justify-between text-sm text-slate-400">
        <button type="button" onClick={onGoToSignup} className="hover:text-slate-200">
          Sign up
        </button>
        <button type="button" onClick={onGoToForgotPassword} className="hover:text-slate-200">
          Forgot password?
        </button>
      </div>
      <div className="mt-2 text-center text-sm text-slate-400">
        <button type="button" onClick={onGoToResendVerification} className="hover:text-slate-200">
          Resend verification email?
        </button>
      </div>
    </div>
  );
}
