interface HeaderProps {
  onClearKey: () => void;
}

/** Dashboard top bar: app title and a button to clear/replace the stored
 * API key. */
export default function Header({ onClearKey }: HeaderProps) {
  return (
    <header className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
      <span className="text-lg font-semibold tracking-tight text-slate-100">Gatekeep</span>
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
