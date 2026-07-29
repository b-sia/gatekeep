import { useEffect, useState } from "react";
import { getPromptVersions } from "../api/client";
import type { PromptOut, PromptVersionOut } from "../api/types";

interface PromptsPanelProps {
  prompts: PromptOut[];
}

export default function PromptsPanel({ prompts }: PromptsPanelProps) {
  const [selected, setSelected] = useState<string>("");
  const [versions, setVersions] = useState<PromptVersionOut[]>([]);

  useEffect(() => {
    if (!selected && prompts.length > 0) {
      setSelected(prompts[0].name);
    }
  }, [prompts, selected]);

  useEffect(() => {
    if (!selected) {
      setVersions([]);
      return;
    }
    let cancelled = false;
    getPromptVersions(selected).then((res) => {
      if (!cancelled) setVersions(res.versions);
    });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  return (
    <div className="mx-6 mb-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium text-slate-300">Prompts</h2>
        <select
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        >
          {prompts.map((prompt) => (
            <option key={prompt.name} value={prompt.name}>
              {prompt.name}
            </option>
          ))}
        </select>
      </div>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Version</th>
            <th className="pb-2">Status</th>
            <th className="pb-2">Created</th>
            <th className="pb-2">Created by</th>
            <th className="pb-2">Notes</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((version) => (
            <tr key={version.version_num} className="border-t border-slate-800">
              <td className="py-2 text-slate-200">v{version.version_num}</td>
              <td className="py-2">
                {version.active && (
                  <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                    Active
                  </span>
                )}
              </td>
              <td className="py-2 text-slate-300">{new Date(version.created_at).toLocaleString()}</td>
              <td className="py-2 text-slate-300">{version.created_by ?? "-"}</td>
              <td className="py-2 text-slate-400">{version.notes ?? "-"}</td>
            </tr>
          ))}
          {versions.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-slate-500">
                {selected ? "No versions yet." : "No prompts registered."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
