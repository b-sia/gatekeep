import { useState } from "react";
import { createAccount } from "../api/client";

interface CreateAccountModalProps {
  onClose: () => void;
  onCreated: () => void;
}

/** Operator modal to create an account: name, optional budget, optional
 * operator flag. Leaving budget blank means unlimited. */
export default function CreateAccountModal({ onClose, onCreated }: CreateAccountModalProps) {
  const [name, setName] = useState("");
  const [budget, setBudget] = useState("");
  const [operator, setOperator] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Submits the create request, mapping a blank budget to unlimited (null). */
  async function handleCreate() {
    setError(null);
    setBusy(true);
    try {
      const trimmed = budget.trim();
      await createAccount({
        name: name.trim(),
        monthly_budget_usd: trimmed === "" ? null : Number(trimmed),
        is_operator: operator,
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create account");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-3 text-base font-semibold text-slate-100">Create account</h2>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="account name"
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <input
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
          placeholder="monthly budget USD (blank = unlimited)"
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <label className="mb-4 flex items-center gap-2 text-sm text-slate-300">
          <input type="checkbox" checked={operator} onChange={(e) => setOperator(e.target.checked)} />
          Operator
        </label>
        {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            onClick={handleCreate}
            disabled={busy || name.trim() === ""}
            className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
