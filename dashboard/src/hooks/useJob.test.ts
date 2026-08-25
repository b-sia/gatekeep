import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useJob } from "./useJob";
import * as client from "../api/client";
import { UnauthorizedError } from "../api/client";
import type { JobStatusResponse } from "../api/types";

function job(status: JobStatusResponse["status"]): JobStatusResponse {
  return {
    id: "j",
    kind: "eval_run",
    prompt_name: "p",
    version_num: null,
    status,
    progress: { done: 0, total: 0 },
    result: null,
    error: null,
    created_at: "",
    updated_at: "",
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  // @testing-library/dom's `waitFor` only recognizes Jest's fake timers
  // (it checks `typeof jest !== "undefined"`), not Vitest's `vi`. Without
  // this shim it falls back to a real `setInterval` for its retry loop,
  // but that interval is itself faked by `vi.useFakeTimers()` and never
  // advances, so `waitFor` calls hang until Vitest's own test timeout.
  // Stubbing a minimal `jest` global routes its retry loop through
  // `vi.advanceTimersByTime` instead.
  (globalThis as unknown as { jest?: { advanceTimersByTime: typeof vi.advanceTimersByTime } }).jest = {
    advanceTimersByTime: vi.advanceTimersByTime,
  };
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete (globalThis as unknown as { jest?: unknown }).jest;
});

describe("useJob", () => {
  it("polls to a terminal status and calls onSettled once", async () => {
    const spy = vi
      .spyOn(client, "getJob")
      .mockResolvedValueOnce(job("running"))
      .mockResolvedValueOnce(job("succeeded"));
    const onSettled = vi.fn();
    const { result } = renderHook(() => useJob("j", { onSettled }));

    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() => expect(result.current.job?.status).toBe("running"));
    await vi.advanceTimersByTimeAsync(1000);
    await waitFor(() => expect(result.current.job?.status).toBe("succeeded"));
    expect(onSettled).toHaveBeenCalledTimes(1);
    // No further polls after terminal.
    await vi.advanceTimersByTimeAsync(3000);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("surfaces an expired job as an error after repeated failures", async () => {
    vi.spyOn(client, "getJob").mockRejectedValue(new Error("job not found or expired"));
    const { result } = renderHook(() => useJob("j"));
    // A single failure is treated as transient and retried silently.
    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() => expect(result.current.error).toBeNull());
    // Only after MAX_CONSECUTIVE_ERRORS in a row does it give up.
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() =>
      expect(result.current.error).toBe("status unavailable, refresh"),
    );
  });

  it("retries silently through a transient failure and keeps polling", async () => {
    const spy = vi
      .spyOn(client, "getJob")
      .mockRejectedValueOnce(new Error("network blip"))
      .mockResolvedValueOnce(job("running"))
      .mockResolvedValueOnce(job("succeeded"));
    const onSettled = vi.fn();
    const { result } = renderHook(() => useJob("j", { onSettled }));

    await vi.advanceTimersByTimeAsync(0);
    // The failed poll doesn't surface an error.
    expect(result.current.error).toBeNull();
    await vi.advanceTimersByTimeAsync(1000);
    await waitFor(() => expect(result.current.job?.status).toBe("running"));
    expect(result.current.error).toBeNull();
    await vi.advanceTimersByTimeAsync(1000);
    await waitFor(() => expect(result.current.job?.status).toBe("succeeded"));
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledTimes(3);
  });

  it("is idle when jobId is null", async () => {
    const spy = vi.spyOn(client, "getJob");
    const { result } = renderHook(() => useJob(null));
    await vi.advanceTimersByTimeAsync(2000);
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.job).toBeNull();
  });

  it("keeps the terminal job snapshot visible after the consumer clears jobId in onSettled", async () => {
    vi.spyOn(client, "getJob")
      .mockResolvedValueOnce(job("running"))
      .mockResolvedValueOnce(job("blocked"));

    // Mirrors VersionsSection/EvalsSection: onSettled clears jobId, which
    // re-renders the hook with jobId=null.
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) =>
        useJob(id, {
          onSettled: () => rerender({ id: null }),
        }),
      { initialProps: { id: "j" as string | null } },
    );

    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() => expect(result.current.job?.status).toBe("running"));
    await vi.advanceTimersByTimeAsync(1000);
    await waitFor(() => expect(result.current.job?.status).toBe("blocked"));

    // The consumer's onSettled has now cleared jobId back to null (via
    // rerender), which re-runs the hook's effect. The terminal snapshot
    // must still be visible, not wiped back to null.
    expect(result.current.job?.status).toBe("blocked");
  });

  it("calls onUnauthorized (not the generic error) on a 401 from getJob", async () => {
    vi.spyOn(client, "getJob").mockRejectedValue(new UnauthorizedError("API key was rejected"));
    const onUnauthorized = vi.fn();
    const { result } = renderHook(() => useJob("j", { onUnauthorized }));

    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(result.current.error).toBeNull();
  });
});
