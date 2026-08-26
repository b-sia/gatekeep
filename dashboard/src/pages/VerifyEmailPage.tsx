import { useEffect, useState } from "react";
import { verifyEmail } from "../api/auth";

interface VerifyEmailPageProps {
  /** Called when the user wants to go to the login page. */
  onGoToLogin?: () => void;
}

type VerifyState = "pending" | "success" | "failure";

/**
 * Email verification page: on mount, reads the `token` query parameter
 * from the URL and calls `verifyEmail()` with it, then shows a
 * success or failure message with a link to sign in.
 *
 * @param props - See {@link VerifyEmailPageProps}.
 */
export default function VerifyEmailPage({ onGoToLogin }: VerifyEmailPageProps) {
  const [state, setState] = useState<VerifyState>("pending");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) {
      setState("failure");
      setError("Missing verification token.");
      return;
    }
    verifyEmail(token)
      .then(() => setState("success"))
      .catch((err) => {
        setState("failure");
        setError(err instanceof Error ? err.message : "Failed to verify email");
      });
  }, []);

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6">
      <h1 className="mb-4 text-lg font-semibold text-slate-100">Verify email</h1>
      {state === "pending" && <p className="text-sm text-slate-300">Verifying your email...</p>}
      {state === "success" && (
        <p className="text-sm text-slate-300">Your email has been verified.</p>
      )}
      {state === "failure" && (
        <p className="text-sm text-red-400">{error ?? "Failed to verify email."}</p>
      )}
      <button
        type="button"
        onClick={onGoToLogin}
        className="mt-4 text-sm text-indigo-400 hover:text-indigo-300"
      >
        Back to sign in
      </button>
    </div>
  );
}
