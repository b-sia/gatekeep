import { useCallback, useState } from "react";
import { UnauthorizedError } from "../api/client";

/**
 * Shared error handling for a dashboard API call: tracks a display-ready
 * error message and redirects to re-auth on 401, so call sites don't each
 * repeat `if (err instanceof UnauthorizedError) return onUnauthorized();
 * setError(...)` in their catch block.
 *
 * @param onUnauthorized - Called instead of setting `error` when the caught
 *   error is a 401 (e.g. to clear the stored key and return to key entry).
 * @returns `error`, the current message (or null); `setError`, to set or
 *   clear it directly (e.g. for local validation before a request is even
 *   sent); and `handleError`, to call from a catch block with the caught
 *   error and a fallback message for non-`Error` throws.
 */
export function useApiErrorHandler(onUnauthorized: () => void) {
  const [error, setError] = useState<string | null>(null);

  const handleError = useCallback(
    (err: unknown, fallback: string) => {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      setError(err instanceof Error ? err.message : fallback);
    },
    [onUnauthorized],
  );

  return { error, setError, handleError };
}
