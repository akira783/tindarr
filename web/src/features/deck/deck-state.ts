/**
 * The deck's decisions, with no React and no network in them (roadmap 4.6).
 *
 * Everything here is a pure function of what the server said and what the user
 * has done, so the two properties that matter can be asserted without a browser:
 * **a vote is never lost** (it stays in the queue, with the same
 * `client_vote_id`, until the server stores it) and **the polling loop always
 * has a way out** (a batch that is ready, an error, or a deadline).
 */
import { DEFAULT_POLL_MS, MAX_POLL_MS, MIN_POLL_MS } from "../../api/poll";
import type { Card, Deck, VoteInput, VoteValue, Waiting } from "../../api/operations";

/** How long the console keeps asking for a batch before it stops and says so. */
export const MAX_WAIT_MS = 180_000;

export type DeckAnswer = Deck | Waiting;

export function isDeck(value: DeckAnswer | undefined): value is Deck {
  return value !== undefined && !("waiting" in value);
}

export function isPendingBatch(value: DeckAnswer | undefined): value is Waiting {
  return value !== undefined && "waiting" in value;
}

/**
 * The deck's own floor, above the console's.
 *
 * `MIN_POLL_MS` is 250 and exists for the pairing poller, which reads a row. A
 * `202` from the deck is a generation in flight, and the contract allows a
 * `retry_after_ms` of 250 — which would be 720 requests in one three-minute wait,
 * against an endpoint that starts paid work when nothing is ready. The server's
 * own default is a second, and a second is what this asks for at the fastest.
 */
export const DECK_MIN_POLL_MS = 1_000;

/** What the server asked to wait, kept inside the bounds the caller polls at. */
export function pollDelay(
  retryAfterMs: number | null | undefined,
  floor: number = MIN_POLL_MS,
): number {
  return Math.min(Math.max(retryAfterMs ?? DEFAULT_POLL_MS, floor), MAX_POLL_MS);
}

/** A vote the user has cast, whether or not the server knows about it yet. */
export interface PendingVote {
  /** Generated once and reused on every retry: this is what makes a resend a `duplicate`. */
  clientVoteId: string;
  vote: VoteValue;
  votedAt: string;
  card: Card;
}

export function toVoteInput(entry: PendingVote): VoteInput {
  return {
    client_vote_id: entry.clientVoteId,
    card_id: entry.card.id,
    vote: entry.vote,
    voted_at: entry.votedAt,
  };
}

/** The cards still to show: the server's order, minus what this session has voted on. */
export function remainingCards(cards: readonly Card[], voted: ReadonlySet<string>): Card[] {
  return cards.filter((card) => !voted.has(card.id));
}

/** A card's runtime, as "1 h 52" or "3 seasons", or nothing when neither is known. */
export function lengthOf(card: Card): { minutes: number } | { seasons: number } | null {
  if (card.media_type === "movie") {
    return card.runtime_minutes == null ? null : { minutes: card.runtime_minutes };
  }
  return card.seasons == null ? null : { seasons: card.seasons };
}

/** The providers worth a badge, the ones the household pays for first. */
export function sortedProviders(card: Card): NonNullable<Card["providers"]> {
  return [...(card.providers ?? [])].sort((a, b) => {
    if (a.subscribed !== b.subscribed) return a.subscribed ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

/** A random id for a vote; `randomUUID` is absent from a few older browsers. */
export function newVoteId(): string {
  const source = globalThis.crypto as Crypto | undefined;
  if (source !== undefined && typeof source.randomUUID === "function") return source.randomUUID();
  const bytes = new Uint8Array(16);
  source?.getRandomValues(bytes);
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
