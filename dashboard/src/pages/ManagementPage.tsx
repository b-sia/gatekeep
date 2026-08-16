import AccountsTable from "../components/AccountsTable";
import BudgetCard from "../components/BudgetCard";
import KeyTable from "../components/KeyTable";
import type { MeResponse } from "../api/types";

interface ManagementPageProps {
  me: MeResponse | null;
  onUnauthorized: () => void;
  onMeChanged: (me: MeResponse) => void;
}

/**
 * Accounts & Keys tab. Every account sees its own budget card and key table;
 * operators additionally see the all-accounts operator section.
 */
export default function ManagementPage({ me, onUnauthorized, onMeChanged }: ManagementPageProps) {
  if (!me) {
    return <p className="mx-6 mt-6 text-sm text-slate-400">Loading account...</p>;
  }
  return (
    <div className="pb-8">
      <BudgetCard me={me} />
      <KeyTable accountId={me.account_id} onUnauthorized={onUnauthorized} />
      {me.is_operator && (
        <AccountsTable
          selfAccountId={me.account_id}
          onMeChanged={onMeChanged}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
