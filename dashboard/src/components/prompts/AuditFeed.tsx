import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getAuditFeed } from "../../api/client";
import type { AuditEventOut } from "../../api/types";

interface AuditFeedProps {
  onUnauthorized: () => void;
  /** When set, scopes the feed to one prompt (entity_ref); else global. */
  entityRef?: string;
}

const RESULT_BADGE: Record<string, string> = {
  success: "bg-emerald-900 text-emerald-300",
  blocked: "bg-amber-900 text-amber-300",
  error: "bg-red-900 text-red-300",
};

/**
 * Read-only audit feed. Shows the newest mutating actions (actor, action,
 * target, result), optionally scoped to a single prompt via `entityRef`.
 */
export default function AuditFeed({ onUnauthorized, entityRef }: AuditFeedProps) {
  const [events, setEvents] = useState<AuditEventOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      // Scoped by entityRef alone (not entityType: "prompt"): eval-suite
      // (eval.create_suite/eval.add_case) and curated-case (curation.review)
      // events also carry entity_ref = the prompt name, just under a
      // different entity_type, so filtering on entityType too would
      // silently drop them from this prompt's feed.
      const res = await getAuditFeed(entityRef ? { entityRef } : { limit: 100 });
      setEvents(res.events);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load audit feed");
    }
  }, [entityRef, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">
          Audit{entityRef ? ` - ${entityRef}` : ""}
        </h3>
        <button
          onClick={load}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          Refresh
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">When</th>
            <th className="pb-2">Actor</th>
            <th className="pb-2">Action</th>
            <th className="pb-2">Target</th>
            <th className="pb-2">Result</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id} className="border-t border-slate-800">
              <td className="py-2 text-slate-300">
                {new Date(e.created_at).toLocaleString()}
              </td>
              <td className="py-2 text-slate-300">{e.actor_label}</td>
              <td className="py-2 text-slate-200">{e.action}</td>
              <td className="py-2 text-slate-300">
                {e.entity_ref ?? "-"}
                {e.version_num !== null ? ` v${e.version_num}` : ""}
              </td>
              <td className="py-2">
                <span
                  className={`rounded px-2 py-0.5 text-xs ${
                    RESULT_BADGE[e.result] ?? "bg-slate-800 text-slate-300"
                  }`}
                >
                  {e.result}
                </span>
              </td>
            </tr>
          ))}
          {events.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-slate-500">
                No audit events yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
