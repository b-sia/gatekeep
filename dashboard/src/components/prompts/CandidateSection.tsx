import { useState } from "react";
import {
  UnauthorizedError,
  clearCandidate,
  setCandidate,
} from "../../api/client";
import type { CandidateResponse, PromptVersionOut } from "../../api/types";

interface CandidateSectionProps {
  name: string;
  versions: PromptVersionOut[];
  onUnauthorized: () => void;
}

/**
 * A/B candidate sub-section: set or adjust the candidate version + traffic
 * percentage, or clear it. Reflects the server's returned candidate config
 * after each action (setting a candidate never runs the eval gate or
 * invalidates cache).
 */
export default function CandidateSection({
  name,
  versions,
  onUnauthorized,
}: CandidateSectionProps) {
  const [versionNum, setVersionNum] = useState<number | "">("");
  const [pct, setPct] = useState<number>(10);
  const [current, setCurrent] = useState<CandidateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const apply = async () => {
    if (versionNum === "") return;
    setError(null);
    try {
      const res = await setCandidate(name, {
        version_num: Number(versionNum),
        traffic_pct: pct,
      });
      setCurrent(res);
    } catch (err) {
      fail(err, "Failed to set candidate");
    }
  };

  const clear = async () => {
    setError(null);
    try {
      const res = await clearCandidate(name);
      setCurrent(res);
    } catch (err) {
      fail(err, "Failed to clear candidate");
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h3 className="mb-3 text-sm font-medium text-slate-300">A/B candidate</h3>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
      {current && (
        <p className="mb-3 text-xs text-slate-400">
          {current.candidate_version_num === null
            ? "No candidate configured (100% active)."
            : `Candidate v${current.candidate_version_num} at ${current.traffic_pct}% traffic.`}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-slate-400">
          Version
          <select
            value={versionNum}
            onChange={(e) => setVersionNum(e.target.value === "" ? "" : Number(e.target.value))}
            className="mt-1 block rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          >
            <option value="">Select...</option>
            {versions.map((v) => (
              <option key={v.version_num} value={v.version_num}>
                v{v.version_num}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          Traffic %
          <input
            type="number"
            min={0}
            max={100}
            value={pct}
            onChange={(e) => setPct(Number(e.target.value))}
            className="mt-1 block w-20 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
        </label>
        <button
          onClick={apply}
          disabled={versionNum === ""}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Set candidate
        </button>
        <button
          onClick={clear}
          className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
