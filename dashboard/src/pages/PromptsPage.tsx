import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getPrompts } from "../api/client";
import { useApiErrorHandler } from "../hooks/useApiErrorHandler";
import type { MeResponse, PromptOut } from "../api/types";
import PromptDetail from "../components/PromptDetail";
import CreatePromptModal from "../components/prompts/CreatePromptModal";
import AuditFeed from "../components/prompts/AuditFeed";

interface PromptsPageProps {
  me: MeResponse | null;
  meError: string | null;
  onRetryMe: () => void;
  onUnauthorized: () => void;
}

/**
 * Prompts tab: master-detail operator console for the full prompt lifecycle.
 * Left pane lists prompts; the right pane shows the selected prompt's
 * versions, A/B candidate, evals, and curation, plus a global audit feed.
 * Gated to operators, matching the Accounts tab; non-operators see a notice.
 */
export default function PromptsPage({
  me,
  meError,
  onRetryMe,
  onUnauthorized,
}: PromptsPageProps) {
  const [prompts, setPrompts] = useState<PromptOut[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const { error, setError, handleError } = useApiErrorHandler(onUnauthorized);

  const loadPrompts = useCallback(async () => {
    setError(null);
    try {
      const res = await getPrompts();
      setPrompts(res.prompts);
      setSelected((cur) => cur ?? res.prompts[0]?.name ?? null);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      handleError(err, "Failed to load prompts");
    }
  }, [setError, handleError, onUnauthorized]);

  useEffect(() => {
    if (me?.is_operator) loadPrompts();
  }, [me?.is_operator, loadPrompts]);

  if (!me) {
    if (meError) {
      return (
        <div className="mx-6 mt-6 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-300">
          <span>{meError}</span>
          <button
            onClick={onRetryMe}
            className="rounded border border-red-800 px-3 py-1 text-xs text-red-200 hover:bg-red-900"
          >
            Retry
          </button>
        </div>
      );
    }
    return <p className="mx-6 mt-6 text-sm text-slate-400">Loading account...</p>;
  }

  if (!me.is_operator) {
    return (
      <p className="mx-6 mt-6 text-sm text-slate-400">
        The Prompts console requires operator access.
      </p>
    );
  }

  return (
    <div className="flex gap-4 px-6 py-6">
      <aside className="w-64 shrink-0">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium text-slate-300">Prompts</h2>
          <button
            onClick={() => setCreating(true)}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          >
            New prompt
          </button>
        </div>
        {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
        <ul className="space-y-1">
          {prompts.map((p) => (
            <li key={p.name}>
              <button
                onClick={() => setSelected(p.name)}
                className={`w-full rounded px-2 py-1.5 text-left text-sm ${
                  selected === p.name
                    ? "bg-slate-800 text-slate-100"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                <span className="block truncate">{p.name}</span>
                <span className="text-xs text-slate-500">
                  {p.active_version_num ? `v${p.active_version_num}` : "no active version"}
                </span>
              </button>
            </li>
          ))}
          {prompts.length === 0 && (
            <li className="text-sm text-slate-500">No prompts registered.</li>
          )}
        </ul>
      </aside>
      <section className="min-w-0 flex-1 space-y-4">
        {selected ? (
          <PromptDetail
            key={selected}
            name={selected}
            onUnauthorized={onUnauthorized}
            onPromptsChanged={loadPrompts}
          />
        ) : (
          <p className="text-sm text-slate-500">Select a prompt to view its details.</p>
        )}
        <AuditFeed onUnauthorized={onUnauthorized} />
      </section>
      {creating && (
        <CreatePromptModal
          onClose={() => setCreating(false)}
          onCreated={(name) => {
            setCreating(false);
            setSelected(name);
            loadPrompts();
          }}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
