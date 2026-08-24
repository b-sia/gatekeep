import { useState } from "react";
import {
  UnauthorizedError,
  addPromptVersion,
  promotePrompt,
  rollbackPrompt,
} from "../../api/client";
import { useJob } from "../../hooks/useJob";
import type { PromptVersionOut } from "../../api/types";

interface VersionsSectionProps {
  name: string;
  versions: PromptVersionOut[];
  onChanged: () => void;
  onUnauthorized: () => void;
}

/**
 * Versions sub-section: the prompt's version timeline (with template text),
 * an "Add version" editor, and per-version Promote / Rollback actions behind
 * a confirm. Promote runs as a background job (eval gate + cache
 * invalidation), surfaced inline via useJob.
 */
export default function VersionsSection({
  name,
  versions,
  onChanged,
  onUnauthorized,
}: VersionsSectionProps) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<
    { kind: "promote" | "rollback"; versionNum: number } | null
  >(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const { job, error: jobError } = useJob(jobId, {
    onSettled: () => {
      setJobId(null);
      onChanged();
    },
  });

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const submitAdd = async () => {
    setError(null);
    try {
      await addPromptVersion(name, { template: draft, notes: notes.trim() || undefined });
      setAdding(false);
      setDraft("");
      setNotes("");
      onChanged();
    } catch (err) {
      fail(err, "Failed to add version");
    }
  };

  const runConfirmed = async () => {
    if (!confirm) return;
    setError(null);
    try {
      if (confirm.kind === "promote") {
        const res = await promotePrompt(name, confirm.versionNum);
        setJobId(res.job_id);
      } else {
        await rollbackPrompt(name);
        onChanged();
      }
    } catch (err) {
      fail(err, `Failed to ${confirm.kind}`);
    } finally {
      setConfirm(null);
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Versions</h3>
        <button
          onClick={() => setAdding((v) => !v)}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
        >
          {adding ? "Cancel" : "Add version"}
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}

      {adding && (
        <div className="mb-4 space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={6}
            placeholder="New template text"
            className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-sm text-slate-200"
          />
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Notes (optional)"
            className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
          <button
            onClick={submitAdd}
            disabled={!draft}
            className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Save version
          </button>
        </div>
      )}

      {jobId && (
        <p className="mb-2 text-xs text-amber-300">
          Promotion job {job?.status ?? "queued"}
          {job?.progress.total ? ` (${job.progress.done}/${job.progress.total})` : ""}...
        </p>
      )}
      {jobError && <p className="mb-2 text-xs text-red-400">{jobError}</p>}
      {job?.status === "blocked" && (
        <p className="mb-2 text-xs text-red-400">
          Promotion blocked by eval gate, score {job.result?.score?.toFixed(2)}.
        </p>
      )}

      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Version</th>
            <th className="pb-2">Status</th>
            <th className="pb-2">Created by</th>
            <th className="pb-2">Template</th>
            <th className="pb-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.version_num} className="border-t border-slate-800 align-top">
              <td className="py-2 text-slate-200">v{v.version_num}</td>
              <td className="py-2">
                {v.active ? (
                  <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                    Active
                  </span>
                ) : (
                  "-"
                )}
              </td>
              <td className="py-2 text-slate-300">{v.created_by ?? "-"}</td>
              <td className="max-w-md py-2">
                <pre className="max-h-24 overflow-auto whitespace-pre-wrap font-mono text-xs text-slate-400">
                  {v.template}
                </pre>
              </td>
              <td className="py-2 text-right">
                {!v.active && (
                  <button
                    onClick={() => setConfirm({ kind: "promote", versionNum: v.version_num })}
                    className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
                  >
                    Promote
                  </button>
                )}
              </td>
            </tr>
          ))}
          {versions.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-slate-500">
                No versions yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="mt-3">
        <button
          onClick={() => setConfirm({ kind: "rollback", versionNum: 0 })}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          Roll back to previous
        </button>
      </div>

      {confirm && (
        <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 p-5">
            <p className="mb-4 text-sm text-slate-200">
              {confirm.kind === "promote"
                ? `Promote v${confirm.versionNum}? This runs the eval gate (if a suite exists) and invalidates this prompt's cache.`
                : "Roll back to the previously-active version? This invalidates this prompt's cache."}
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirm(null)}
                className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                onClick={runConfirmed}
                className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500"
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
