import { useCallback, useEffect, useState } from "react";
import {
  UnauthorizedError,
  getPromptVersions,
} from "../api/client";
import type { PromptVersionOut } from "../api/types";
import VersionsSection from "./prompts/VersionsSection";
import CandidateSection from "./prompts/CandidateSection";
import EvalsSection from "./prompts/EvalsSection";
import CurationSection from "./prompts/CurationSection";

interface PromptDetailProps {
  name: string;
  onUnauthorized: () => void;
  /** Refresh the master list (e.g. after a promote changes active version). */
  onPromptsChanged: () => void;
}

/**
 * Right-pane container for a selected prompt: loads its version timeline and
 * fans out to the Versions, A/B candidate, Evals, and Curation sub-sections.
 */
export default function PromptDetail({
  name,
  onUnauthorized,
  onPromptsChanged,
}: PromptDetailProps) {
  const [versions, setVersions] = useState<PromptVersionOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadVersions = useCallback(async () => {
    setError(null);
    try {
      const res = await getPromptVersions(name);
      setVersions(res.versions);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load versions");
    }
  }, [name, onUnauthorized]);

  useEffect(() => {
    loadVersions();
  }, [loadVersions]);

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold text-slate-100">{name}</h2>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <VersionsSection
        name={name}
        versions={versions}
        onChanged={() => {
          loadVersions();
          onPromptsChanged();
        }}
        onUnauthorized={onUnauthorized}
      />
      <CandidateSection
        name={name}
        versions={versions}
        onUnauthorized={onUnauthorized}
      />
      <EvalsSection name={name} versions={versions} onUnauthorized={onUnauthorized} />
      <CurationSection name={name} onUnauthorized={onUnauthorized} />
    </div>
  );
}
