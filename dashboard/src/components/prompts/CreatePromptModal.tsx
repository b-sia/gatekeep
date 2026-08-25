import { useState } from "react";
import { UnauthorizedError, createPrompt } from "../../api/client";

interface CreatePromptModalProps {
  onClose: () => void;
  onCreated: (name: string) => void;
  onUnauthorized: () => void;
}

/** Modal to create a new prompt with its initial (active) version 1. */
export default function CreatePromptModal({
  onClose,
  onCreated,
  onUnauthorized,
}: CreatePromptModalProps) {
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    setBusy(true);
    try {
      const res = await createPrompt({
        name: name.trim(),
        template,
        notes: notes.trim() || undefined,
      });
      onCreated(res.name);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to create prompt");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-lg border border-slate-800 bg-slate-900 p-5">
        <h3 className="mb-3 text-sm font-medium text-slate-200">New prompt</h3>
        {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
        <label className="mb-1 block text-xs text-slate-400">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        />
        <label className="mb-1 block text-xs text-slate-400">Template</label>
        <textarea
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          rows={6}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-sm text-slate-200"
        />
        <label className="mb-1 block text-xs text-slate-400">Notes (optional)</label>
        <input
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={busy || !name.trim() || !template}
            className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
