import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useJob } from "./useJob";
import * as client from "../api/client";
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

  it("surfaces an expired job as an error", async () => {
    vi.spyOn(client, "getJob").mockRejectedValue(new Error("job not found or expired"));
    const { result } = renderHook(() => useJob("j"));
    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() =>
      expect(result.current.error).toBe("status unavailable, refresh"),
    );
  });

  it("is idle when jobId is null", async () => {
    const spy = vi.spyOn(client, "getJob");
    const { result } = renderHook(() => useJob(null));
    await vi.advanceTimersByTimeAsync(2000);
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.job).toBeNull();
  });
});
