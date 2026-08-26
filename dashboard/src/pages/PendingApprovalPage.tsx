import { useState } from "react";
import { logout } from "../api/auth";

/**
 * Static page shown to accounts that have signed up and verified their
 * email but are still awaiting operator approval. Offers a log out
 * button that clears the session and reloads the app.
 */
export default function PendingApprovalPage() {
  const [busy, setBusy] = useState(false);

  /**
   * Logs out the current session and reloads the page so the app
   * re-evaluates auth state from scratch.
   */
  async function handleLogout() {
    setBusy(true);
    try {
      await logout();
    } finally {
      window.location.reload();
    }
  }

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6 text-center">
      <h1 className="mb-4 text-lg font-semibold text-slate-100">Pending approval</h1>
      <p className="text-sm text-slate-300">Your account is awaiting approval.</p>
      <button
        type="button"
        onClick={handleLogout}
        disabled={busy}
        className="mt-6 rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800 disabled:opacity-50"
      >
        Log out
      </button>
    </div>
  );
}
