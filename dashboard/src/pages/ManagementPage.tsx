import AccountsTable from "../components/AccountsTable";
import BudgetCard from "../components/BudgetCard";
import KeyTable from "../components/KeyTable";
import PendingRequestsPanel from "../components/PendingRequestsPanel";
import type { MeResponse } from "../api/types";

interface ManagementPageProps {
  me: MeResponse | null;
  /** Set when the initial GET /me failed for a reason other than 401
   * (5xx, network error), so the page can offer a retry instead of
   * spinning on "Loading account..." forever. */
  meError: string | null;
  onRetryMe: () => void;
  onUnauthorized: () => void;
  onMeChanged: (me: MeResponse) => void;
}

/**
 * Accounts & Keys tab. Every account sees its own budget card and key table;
 * operators additionally see the all-accounts operator section.
 */
export default function ManagementPage({
  me,
  meError,
  onRetryMe,
  onUnauthorized,
  onMeChanged,
}: ManagementPageProps) {
  if (!me) {
    if (meError) {
      return (
        <div className="mx-6 mt-6 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-300">
          <span>{meError}</span>
          <button
            onClick={onRetryMe}
            className="rounded border border-red-800 px-3 py-1 text-xs text-red-200 hover:bg-red-900"
          >
            Retry
          </button>
        </div>
      );
    }
    return <p className="mx-6 mt-6 text-sm text-slate-400">Loading account...</p>;
  }
  return (
    <div className="pb-8">
      <BudgetCard me={me} />
      <KeyTable accountId={me.account_id} onUnauthorized={onUnauthorized} />
      {me.is_operator && <PendingRequestsPanel />}
      {me.is_operator && (
        <AccountsTable
          selfAccountId={me.account_id}
          selfStatus={me.status}
          onMeChanged={onMeChanged}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
