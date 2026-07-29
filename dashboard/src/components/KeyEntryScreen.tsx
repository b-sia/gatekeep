import { useState, type FormEvent } from "react";
import { setStoredApiKey } from "../api/client";

interface KeyEntryScreenProps {
  onKeySaved: () => void;
}

/** One-time entry screen prompting for a Gatekeep API key before the
 * dashboard can load; saves the key to localStorage on submit. */
export default function KeyEntryScreen({ onKeySaved }: KeyEntryScreenProps) {
  const [value, setValue] = useState("");

  /** Trims and persists the entered key, then notifies the parent, unless
   * the field is empty. */
  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    setStoredApiKey(trimmed);
    onKeySaved();
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6 shadow-xl"
      >
        <h1 className="mb-1 text-lg font-semibold text-slate-100">Gatekeep</h1>
        <p className="mb-4 text-sm text-slate-400">
          Enter your Gatekeep API key to view the dashboard.
        </p>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="sk-..."
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <button
          type="submit"
          className="w-full rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500"
        >
          Continue
        </button>
      </form>
    </div>
  );
}
