import { useCallback, useEffect, useState } from "react";
import {
  UnauthorizedError,
  addCase,
  createSuite,
  getEvalHistory,
  getPromptSuite,
  runEval,
} from "../../api/client";
import { useJob } from "../../hooks/useJob";
import type {
  EvalCaseOut,
  EvalRunOut,
  PromptVersionOut,
  SuiteOut,
} from "../../api/types";

interface EvalsSectionProps {
  name: string;
  versions: PromptVersionOut[];
  onUnauthorized: () => void;
}

/**
 * Evals sub-section: shows the prompt's eval suite and cases (or offers to
 * create a suite), a "Run eval" action (background job with inline
 * progress), and the eval-run history for this prompt.
 */
export default function EvalsSection({
  name,
  versions: _versions,
  onUnauthorized,
}: EvalsSectionProps) {
  const [suite, setSuite] = useState<SuiteOut | null>(null);
  const [cases, setCases] = useState<EvalCaseOut[]>([]);
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const load = useCallback(async () => {
    setError(null);
    try {
      const [suiteRes, historyRes] = await Promise.all([
        getPromptSuite(name),
        getEvalHistory(name),
      ]);
      setSuite(suiteRes.suite);
      setCases(suiteRes.cases);
      setRuns(historyRes.runs);
    } catch (err) {
      fail(err, "Failed to load evals");
    }
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
  }, [load]);

  const { job, error: jobError } = useJob(jobId, {
    onSettled: () => {
      setJobId(null);
      load();
    },
    onUnauthorized,
  });

  const create = async () => {
    try {
      await createSuite(name, {});
      load();
    } catch (err) {
      fail(err, "Failed to create suite");
    }
  };

  const run = async () => {
    try {
      const res = await runEval(name, {});
      setJobId(res.job_id);
    } catch (err) {
      fail(err, "Failed to start eval run");
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Evals</h3>
        {suite ? (
          <button
            onClick={run}
            disabled={!!jobId}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Run eval
          </button>
        ) : (
          <button
            onClick={create}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          >
            Create suite
          </button>
        )}
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}

      {suite ? (
        <>
          <p className="mb-2 text-xs text-slate-400">
            Threshold {suite.pass_threshold} - {cases.length} case(s)
          </p>
          {jobId && (
            <p className="mb-2 text-xs text-amber-300">
              Eval job {job?.status ?? "queued"}
              {job?.progress.total ? ` (${job.progress.done}/${job.progress.total})` : ""}...
            </p>
          )}
          {jobError && <p className="mb-2 text-xs text-red-400">{jobError}</p>}
          <table className="mb-4 w-full text-left text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-slate-500">
                <th className="pb-2">When</th>
                <th className="pb-2">Version</th>
                <th className="pb-2 text-right">Score</th>
                <th className="pb-2">Result</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-t border-slate-800">
                  <td className="py-2 text-slate-300">
                    {new Date(r.created_at).toLocaleString()}
                  </td>
                  <td className="py-2 text-slate-300">v{r.version_num}</td>
                  <td className="py-2 text-right text-slate-300">{r.score.toFixed(2)}</td>
                  <td className="py-2">
                    {r.passed ? (
                      <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                        Pass
                      </span>
                    ) : (
                      <span className="rounded bg-red-900 px-2 py-0.5 text-xs text-red-300">
                        Fail
                      </span>
                    )}
                  </td>
                </tr>
              ))}
              {runs.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-3 text-center text-slate-500">
                    No eval runs yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <AddCaseForm name={name} onAdded={load} onUnauthorized={onUnauthorized} />
        </>
      ) : (
        <p className="text-sm text-slate-500">No eval suite registered.</p>
      )}
    </div>
  );
}

/** Inline form to add a reviewed manual eval case (contains/exact/llm_judge). */
function AddCaseForm({
  name,
  onAdded,
  onUnauthorized,
}: {
  name: string;
  onAdded: () => void;
  onUnauthorized: () => void;
}) {
  const [content, setContent] = useState("");
  const [checkType, setCheckType] = useState("contains");
  const [expected, setExpected] = useState("");
  const [criteria, setCriteria] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    try {
      await addCase(name, {
        input_messages: [{ role: "user", content }],
        check_type: checkType,
        expected: checkType === "llm_judge" ? undefined : expected,
        judge_criteria: checkType === "llm_judge" ? criteria : undefined,
      });
      setContent("");
      setExpected("");
      setCriteria("");
      onAdded();
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to add case");
    }
  };

  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <h4 className="text-xs font-medium text-slate-400">Add case</h4>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <input
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="User message"
        className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
      />
      <div className="flex flex-wrap gap-2">
        <select
          value={checkType}
          onChange={(e) => setCheckType(e.target.value)}
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        >
          <option value="contains">contains</option>
          <option value="icontains">icontains</option>
          <option value="exact">exact</option>
          <option value="llm_judge">llm_judge</option>
        </select>
        {checkType === "llm_judge" ? (
          <input
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            placeholder="Judge criteria"
            className="flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
        ) : (
          <input
            value={expected}
            onChange={(e) => setExpected(e.target.value)}
            placeholder="Expected"
            className="flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
        )}
        <button
          onClick={submit}
          disabled={!content}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Add
        </button>
      </div>
    </div>
  );
}
