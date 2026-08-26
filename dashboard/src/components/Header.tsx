export type TabKey = "analytics" | "management" | "prompts";

interface HeaderProps {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  /** The logged-in account's display name, shown in the indicator. */
  accountName: string;
  /** Whether the logged-in account is an operator, shown as a badge. */
  isOperator: boolean;
  /** Logs the session out, returning to the login page. */
  onLogout: () => void;
}

/**
 * Dashboard top bar: app title, an Analytics / Accounts & Keys / Prompts tab
 * control, the logged-in account indicator, and a Log out button that ends
 * the session and returns to the login page.
 *
 * @param props - See {@link HeaderProps}.
 */
export default function Header({
  activeTab,
  onTabChange,
  accountName,
  isOperator,
  onLogout,
}: HeaderProps) {
  const tabClass = (tab: TabKey) =>
    `rounded px-3 py-1.5 text-sm ${
      activeTab === tab
        ? "bg-slate-800 text-slate-100"
        : "text-slate-400 hover:text-slate-200"
    }`;

  return (
    <header className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
      <div className="flex items-center gap-4">
        <span className="text-lg font-semibold tracking-tight text-slate-100">Gatekeep</span>
        <nav className="flex items-center gap-1">
          <button className={tabClass("analytics")} onClick={() => onTabChange("analytics")}>
            Analytics
          </button>
          <button className={tabClass("management")} onClick={() => onTabChange("management")}>
            Accounts &amp; Keys
          </button>
          <button className={tabClass("prompts")} onClick={() => onTabChange("prompts")}>
            Prompts
          </button>
        </nav>
      </div>
      <div className="flex items-center gap-3">
        <span className="flex items-center gap-2 text-sm text-slate-300">
          {accountName}
          {isOperator && (
            <span className="rounded bg-indigo-600 px-1.5 py-0.5 text-xs text-white">
              operator
            </span>
          )}
        </span>
        <button
          onClick={onLogout}
          title="Log out"
          className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Log out
        </button>
      </div>
    </header>
  );
}
