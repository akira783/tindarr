import { ApiError, isApiError } from "./problem";

export const MIN_POLL_MS = 250;
export const MAX_POLL_MS = 10_000;
export const DEFAULT_POLL_MS = 2_000;

export type PollStep<T> = { done: true; value: T } | { done: false; retryAfterMs?: number | null };

export class PollAborted extends Error {
  constructor() {
    super("polling aborted");
    this.name = "PollAborted";
  }
}

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted === true) {
      reject(new PollAborted());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new PollAborted());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function wait(retryAfterMs: number | null | undefined): number {
  const value = retryAfterMs ?? DEFAULT_POLL_MS;
  return Math.min(Math.max(value, MIN_POLL_MS), MAX_POLL_MS);
}

export interface PollOptions {
  signal?: AbortSignal;
  /** Stop and raise the flow's expiry error once this instant has passed. */
  expiresAt?: string | number | null;
  /** Error raised when `expiresAt` passes; defaults to the step's own. */
  onExpired?: () => Error;
}

/**
 * Polls `step` until it is done, waiting what the server asked for.
 *
 * Stops as soon as the signal aborts (a closed dialog, an unmounted page) and
 * when the flow's own deadline passed, so a forgotten Plex PIN or Quick Connect
 * code never keeps a timer alive.
 */
export async function pollUntilDone<T>(
  step: (signal?: AbortSignal) => Promise<PollStep<T>>,
  options: PollOptions = {},
): Promise<T> {
  const { signal, expiresAt, onExpired } = options;
  const deadline =
    expiresAt == null
      ? null
      : typeof expiresAt === "number"
        ? expiresAt
        : new Date(expiresAt).getTime();

  for (;;) {
    if (signal?.aborted === true) throw new PollAborted();
    if (deadline !== null && Date.now() > deadline) {
      throw onExpired?.() ?? new ApiError(410, "unknown", null);
    }

    let outcome: PollStep<T>;
    try {
      outcome = await step(signal);
    } catch (error) {
      // A rate limit is a "come back later", not a failure of the flow.
      if (isApiError(error) && error.code === "rate_limited") {
        await sleep(wait(error.retryAfterMs), signal);
        continue;
      }
      throw error;
    }

    if (outcome.done) return outcome.value;
    await sleep(wait(outcome.retryAfterMs), signal);
  }
}
