import { useCallback, useEffect, useState } from "react";
import {
  UnauthorizedError,
  getPromptCuration,
  mineCuration,
  reviewCase,
} from "../../api/client";
import type { EvalCaseOut } from "../../api/types";

interface CurationSectionProps {
  name: string;
  onUnauthorized: () => void;
}

/**
 * Curation sub-section: "Mine samples" turns recent traffic into unreviewed
 * candidate cases; each is approved (kept, marked reviewed) or rejected
 * (deleted) inline.
 */
export default function CurationSection({ name, onUnauthorized }: CurationSectionProps) {
  const [cases, setCases] = useState<EvalCaseOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getPromptCuration(name);
      setCases(res.cases);
    } catch (err) {
      fail(err, "Failed to load curation");
    }
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
  }, [load]);

  const mine = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await mineCuration(name, { limit: 20 });
      setCases(res.cases);
    } catch (err) {
      fail(err, "Failed to mine samples");
    } finally {
      setBusy(false);
    }
  };

  const review = async (caseId: number, approved: boolean) => {
    try {
      await reviewCase(name, caseId, approved);
      setCases((cur) => cur.filter((c) => c.id !== caseId));
    } catch (err) {
      fail(err, "Failed to review case");
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Curation</h3>
        <button
          onClick={mine}
          disabled={busy}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Mining..." : "Mine samples"}
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
      <ul className="space-y-2">
        {cases.map((c) => (
          <li key={c.id} className="rounded border border-slate-800 p-2">
            <pre className="mb-2 max-h-24 overflow-auto whitespace-pre-wrap font-mono text-xs text-slate-400">
              {JSON.stringify(c.input_messages, null, 2)}
            </pre>
            {c.judge_criteria && (
              <p className="mb-2 text-xs text-slate-400">Rubric: {c.judge_criteria}</p>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => review(c.id, true)}
                className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600"
              >
                Approve
              </button>
              <button
                onClick={() => review(c.id, false)}
                className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
              >
                Reject
              </button>
            </div>
          </li>
        ))}
        {cases.length === 0 && (
          <li className="text-sm text-slate-500">No unreviewed curated cases.</li>
        )}
      </ul>
    </div>
  );
}
