/**
 * Verdicts the server has not stored yet, kept **outside the component**.
 *
 * `useDeck` used to hold them in state, which meant that opening the connectors
 * page with a verdict still queued — a submit that failed, a connection that
 * dropped — threw it away without a word, in a module whose whole claim is that
 * a vote is never lost. A module-level store is the smallest thing that outlives
 * the page while staying inside the tab: it is not persisted, because a vote is
 * only ever worth resending to the session that cast it.
 *
 * The queue is emptied by a stored answer, by an undo, and by signing out.
 */
import type { PendingVote } from "./deck-state";

let queue: PendingVote[] = [];
/** Verdicts a submit has already been attempted for: see `useDeck`'s undo. */
let attempted = new Set<string>();

export function getVoteQueue(): PendingVote[] {
  return queue;
}

export function setVoteQueue(next: PendingVote[]): void {
  queue = next;
}

export function markAttempted(ids: readonly string[]): void {
  for (const id of ids) attempted.add(id);
}

/**
 * True when a submit carrying this verdict has actually gone out.
 *
 * A submit can fail on the way back — a timeout, a proxy's `502`, a reset after
 * the write — with the vote already stored. "It never reached the server" is
 * only safe to assume for one that was never sent.
 */
export function wasAttempted(clientVoteId: string): boolean {
  return attempted.has(clientVoteId);
}

export function resetVoteQueue(): void {
  queue = [];
  attempted = new Set<string>();
}
