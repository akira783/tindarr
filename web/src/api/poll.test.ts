import { describe, expect, it, vi } from "vitest";

import { MIN_POLL_MS, PollAborted, pollUntilDone, sleep } from "./poll";
import { ApiError } from "./problem";

async function runTimers(): Promise<void> {
  await vi.advanceTimersByTimeAsync(60_000);
}

describe("pollUntilDone", () => {
  it("stops as soon as the step is done", async () => {
    vi.useFakeTimers();
    const step = vi
      .fn()
      .mockResolvedValueOnce({ done: false, retryAfterMs: 1000 })
      .mockResolvedValueOnce({ done: true, value: "signed in" });

    const promise = pollUntilDone<string>(step);
    await runTimers();

    await expect(promise).resolves.toBe("signed in");
    expect(step).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("stops polling when the caller aborts, and makes no further call", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const step = vi.fn().mockResolvedValue({ done: false, retryAfterMs: 1000 });

    const promise = pollUntilDone(step, { signal: controller.signal });
    const assertion = expect(promise).rejects.toBeInstanceOf(PollAborted);
    await vi.advanceTimersByTimeAsync(1000);
    const callsBefore = step.mock.calls.length;
    controller.abort();
    await runTimers();

    await assertion;
    expect(step.mock.calls.length).toBeLessThanOrEqual(callsBefore + 1);
    vi.useRealTimers();
  });

  it("gives up once the flow expired, with the flow's own error", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-22T10:00:00Z"));
    const step = vi.fn().mockResolvedValue({ done: false, retryAfterMs: 1000 });

    const promise = pollUntilDone(step, {
      expiresAt: "2026-09-22T10:00:02Z",
      onExpired: () => new ApiError(410, "quick_connect_expired", null),
    });
    const assertion = expect(promise).rejects.toMatchObject({ code: "quick_connect_expired" });
    await runTimers();

    await assertion;
    vi.useRealTimers();
  });

  it("waits out a rate limit instead of failing", async () => {
    vi.useFakeTimers();
    const step = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError(429, "rate_limited", {
          type: "about:blank",
          title: "slow down",
          status: 429,
          code: "rate_limited",
          retry_after_ms: 3000,
        }),
      )
      .mockResolvedValueOnce({ done: true, value: 7 });

    const promise = pollUntilDone<number>(step);
    await runTimers();

    await expect(promise).resolves.toBe(7);
    expect(step).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("propagates any other error", async () => {
    const step = vi.fn().mockRejectedValue(new ApiError(410, "pin_expired", null));
    await expect(pollUntilDone(step)).rejects.toMatchObject({ code: "pin_expired" });
  });
});

describe("sleep", () => {
  it("rejects at once when the signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(sleep(MIN_POLL_MS, controller.signal)).rejects.toBeInstanceOf(PollAborted);
  });
});
