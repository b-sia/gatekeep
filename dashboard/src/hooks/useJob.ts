import { useEffect, useRef, useState } from "react";
import { UnauthorizedError, getJob } from "../api/client";
import type { JobStatusResponse } from "../api/types";

const TERMINAL: ReadonlySet<string> = new Set(["succeeded", "failed", "blocked"]);
const POLL_INTERVAL_MS = 1000;
const MAX_CONSECUTIVE_ERRORS = 3;

/**
 * Polls a background job's status until it reaches a terminal state.
 *
 * While `jobId` names a job in `queued`/`running`, this re-fetches
 * `GET /prompts/jobs/{id}` every second and returns the latest snapshot.
 * Polling stops as soon as the status is terminal (`succeeded`, `failed`,
 * `blocked`), at which point `onSettled` fires exactly once. A rejected
 * fetch that is a 401 (`UnauthorizedError`) stops polling immediately and
 * calls `onUnauthorized` instead of surfacing an error. Any other rejection
 * (e.g. a transient network blip) is treated as retryable: polling
 * continues silently on the same interval, and `error` is only set once
 * `MAX_CONSECUTIVE_ERRORS` polls in a row have failed (a successful poll in
 * between resets the count) - at that point polling stops and
 * `error = "status unavailable, refresh"`. Passing `jobId === null` leaves
 * the hook idle (no polling).
 *
 * The last terminal snapshot is retained even after the caller clears
 * `jobId` back to null (typically from inside `onSettled`, to reset for the
 * next run) - `job` is only reset to null when a genuinely new (different)
 * non-null `jobId` starts polling, so a terminal result (e.g. an eval-gate
 * "blocked" score) stays visible on screen until the next job begins,
 * rather than being wiped on the very next render.
 *
 * @param jobId - The job to poll, or null to stay idle.
 * @param opts.onSettled - Called once with the terminal job snapshot, e.g.
 *   to refetch the panel the job mutated.
 * @param opts.onUnauthorized - Called when a poll fails with a 401, e.g. to
 *   route back to the identity picker.
 * @returns The latest job snapshot (or null before any job has ever polled)
 *   and an error message (or null).
 */
export function useJob(
  jobId: string | null,
  opts?: {
    onSettled?: (job: JobStatusResponse) => void;
    onUnauthorized?: () => void;
  },
): { job: JobStatusResponse | null; error: string | null } {
  const [job, setJob] = useState<JobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Keep the latest callbacks without making them polling dependencies.
  const onSettledRef = useRef(opts?.onSettled);
  onSettledRef.current = opts?.onSettled;
  const onUnauthorizedRef = useRef(opts?.onUnauthorized);
  onUnauthorizedRef.current = opts?.onUnauthorized;
  // Tracks the id a polling session was last started for, so `job` is reset
  // only when a genuinely new job starts - not when the caller merely clears
  // jobId back to null after a terminal result (see docstring above).
  const activeJobIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!jobId) {
      setError(null);
      return;
    }
    if (activeJobIdRef.current !== jobId) {
      setJob(null);
    }
    activeJobIdRef.current = jobId;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let consecutiveErrors = 0;

    const poll = async () => {
      try {
        const snapshot = await getJob(jobId);
        if (cancelled) return;
        consecutiveErrors = 0;
        setJob(snapshot);
        if (TERMINAL.has(snapshot.status)) {
          onSettledRef.current?.(snapshot);
          return; // stop scheduling
        }
        setError(null);
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof UnauthorizedError) {
          onUnauthorizedRef.current?.();
          return;
        }
        consecutiveErrors += 1;
        if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
          setError("status unavailable, refresh");
          return;
        }
        // Transient failure - keep polling silently rather than giving up.
        timer = setTimeout(poll, POLL_INTERVAL_MS);
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
