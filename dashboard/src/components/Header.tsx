export type TabKey = "analytics" | "management";

interface HeaderProps {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  onClearKey: () => void;
}

/** Dashboard top bar: app title, an Analytics / Accounts & Keys tab control,
 * and a button to clear/replace the stored API key. */
export default function Header({ activeTab, onTabChange, onClearKey }: HeaderProps) {
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
        </nav>
      </div>
      <button
        onClick={onClearKey}
        title="Replace or clear stored API key"
        className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
      >
        API key
      </button>
    </header>
  );
}
