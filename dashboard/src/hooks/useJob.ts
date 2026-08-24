import { useEffect, useRef, useState } from "react";
import { getJob } from "../api/client";
import type { JobStatusResponse } from "../api/types";

const TERMINAL: ReadonlySet<string> = new Set(["succeeded", "failed", "blocked"]);
const POLL_INTERVAL_MS = 1000;

/**
 * Polls a background job's status until it reaches a terminal state.
 *
 * While `jobId` names a job in `queued`/`running`, this re-fetches
 * `GET /prompts/jobs/{id}` every second and returns the latest snapshot.
 * Polling stops as soon as the status is terminal (`succeeded`, `failed`,
 * `blocked`), at which point `onSettled` fires exactly once. A rejected
 * fetch (e.g. the job TTL lapsed => 404) stops polling and surfaces
 * `error = "status unavailable, refresh"`. Passing `jobId === null` leaves
 * the hook idle (no polling, `job === null`).
 *
 * @param jobId - The job to poll, or null to stay idle.
 * @param opts.onSettled - Called once with the terminal job snapshot, e.g.
 *   to refetch the panel the job mutated.
 * @returns The latest job snapshot (or null) and an error message (or null).
 */
export function useJob(
  jobId: string | null,
  opts?: { onSettled?: (job: JobStatusResponse) => void },
): { job: JobStatusResponse | null; error: string | null } {
  const [job, setJob] = useState<JobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Keep the latest onSettled without making it a polling dependency.
  const onSettledRef = useRef(opts?.onSettled);
  onSettledRef.current = opts?.onSettled;

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setError(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const snapshot = await getJob(jobId);
        if (cancelled) return;
        setJob(snapshot);
        if (TERMINAL.has(snapshot.status)) {
          onSettledRef.current?.(snapshot);
          return; // stop scheduling
        }
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch {
        if (cancelled) return;
        setError("status unavailable, refresh");
      }
    };

    setError(null);
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  return { job, error };
}
