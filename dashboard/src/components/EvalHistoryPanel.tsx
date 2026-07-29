import type { EvalRunOut } from "../api/types";

interface EvalHistoryPanelProps {
  runs: EvalRunOut[];
}

export default function EvalHistoryPanel({ runs }: EvalHistoryPanelProps) {
  return (
    <div className="mx-6 mb-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Eval run history</h2>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">When</th>
            <th className="pb-2">Prompt</th>
            <th className="pb-2">Version</th>
            <th className="pb-2">Model</th>
            <th className="pb-2 text-right">Score</th>
            <th className="pb-2">Result</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-t border-slate-800">
              <td className="py-2 text-slate-300">{new Date(run.created_at).toLocaleString()}</td>
              <td className="py-2 text-slate-200">{run.prompt_name}</td>
              <td className="py-2 text-slate-300">v{run.version_num}</td>
              <td className="py-2 text-slate-300">{run.model}</td>
              <td className="py-2 text-right text-slate-300">{run.score.toFixed(2)}</td>
              <td className="py-2">
                {run.passed ? (
                  <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                    Pass
                  </span>
                ) : (
                  <span className="rounded bg-red-900 px-2 py-0.5 text-xs text-red-300">Fail</span>
                )}
              </td>
            </tr>
          ))}
          {runs.length === 0 && (
            <tr>
              <td colSpan={6} className="py-4 text-center text-slate-500">
                No eval runs yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
