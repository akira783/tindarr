import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { DEFAULT_POLL_MS, MAX_POLL_MS, MIN_POLL_MS } from "../../api/poll";
import {
  isDeck,
  isPendingBatch,
  lengthOf,
  newVoteId,
  pollDelay,
  remainingCards,
  sortedProviders,
  toVoteInput,
} from "./deck-state";
import { actionForKey, SHORTCUTS, VERDICTS } from "./keys";

describe("telling a deck from a batch being built", () => {
  it("reads a `200` as a deck and a `202` as a wait", () => {
    expect(isDeck(fixtures.deck())).toBe(true);
    expect(isPendingBatch(fixtures.deck())).toBe(false);
    expect(isPendingBatch({ waiting: true, retryAfterMs: 1000 })).toBe(true);
    expect(isDeck(undefined)).toBe(false);
  });
});

describe("how long to wait before asking again", () => {
  it("does what the server asked", () => {
    expect(pollDelay(1500)).toBe(1500);
  });

  it("never spins faster than the floor nor sleeps past the ceiling", () => {
    expect(pollDelay(1)).toBe(MIN_POLL_MS);
    expect(pollDelay(3_600_000)).toBe(MAX_POLL_MS);
  });

  it("falls back to the default when the server said nothing", () => {
    expect(pollDelay(null)).toBe(DEFAULT_POLL_MS);
    expect(pollDelay(undefined)).toBe(DEFAULT_POLL_MS);
  });
});

describe("the cards still in hand", () => {
  it("keeps the server's order and drops what this session voted on", () => {
    const cards = [
      fixtures.card({ id: "a" }),
      fixtures.card({ id: "b" }),
      fixtures.card({ id: "c" }),
    ];
    expect(remainingCards(cards, new Set(["b"])).map((one) => one.id)).toEqual(["a", "c"]);
  });
});

describe("a vote on its way to the server", () => {
  it("carries the card id, the word and the moment it was cast", () => {
    const entry = {
      clientVoteId: "11111111-1111-4111-8111-111111111111",
      vote: "like" as const,
      votedAt: "2026-09-24T10:00:00.000Z",
      card: fixtures.card({ id: "card-9" }),
    };
    expect(toVoteInput(entry)).toEqual({
      client_vote_id: entry.clientVoteId,
      card_id: "card-9",
      vote: "like",
      voted_at: entry.votedAt,
    });
  });

  it("gives every vote its own id, which is what makes a resend a duplicate", () => {
    const ids = new Set(Array.from({ length: 50 }, () => newVoteId()));
    expect(ids.size).toBe(50);
    for (const id of ids) {
      expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    }
  });
});

describe("how long a title is", () => {
  it("reads minutes for a film and seasons for a series", () => {
    expect(lengthOf(fixtures.card({ runtime_minutes: 148 }))).toEqual({ minutes: 148 });
    expect(lengthOf(fixtures.card({ media_type: "tv", seasons: 3 }))).toEqual({ seasons: 3 });
  });

  it("says nothing rather than zero when the length is unknown", () => {
    expect(lengthOf(fixtures.card({ runtime_minutes: null }))).toBeNull();
    expect(lengthOf(fixtures.card({ media_type: "tv", seasons: null }))).toBeNull();
  });
});

describe("the provider badges", () => {
  it("puts the services the household pays for first", () => {
    const card = fixtures.card({
      providers: [
        { provider_id: 2, name: "Apple TV", logo_path: null, offer: "rent", subscribed: false },
        { provider_id: 8, name: "Netflix", logo_path: null, offer: "subscription", subscribed: true },
      ],
    });
    expect(sortedProviders(card).map((one) => one.name)).toEqual(["Netflix", "Apple TV"]);
  });
});

describe("the keyboard", () => {
  it("maps the four directions to the four verdicts and the letters to the rest", () => {
    expect(actionForKey({ key: "ArrowRight" })).toBe("like");
    expect(actionForKey({ key: "ArrowLeft" })).toBe("dislike");
    expect(actionForKey({ key: "ArrowUp" })).toBe("seen_liked");
    expect(actionForKey({ key: "ArrowDown" })).toBe("seen_disliked");
    expect(actionForKey({ key: "n" })).toBe("skip");
    expect(actionForKey({ key: "U" })).toBe("undo");
    expect(actionForKey({ key: "t" })).toBe("trailer");
  });

  it("gives every verdict a shortcut to announce", () => {
    for (const verdict of VERDICTS) expect(SHORTCUTS[verdict]).not.toBe("");
  });

  it("keeps out of the browser's own shortcuts and of what is being typed", () => {
    expect(actionForKey({ key: "n", ctrlKey: true })).toBeNull();
    expect(actionForKey({ key: "ArrowRight", metaKey: true })).toBeNull();
    expect(actionForKey({ key: "t", altKey: true })).toBeNull();
    expect(actionForKey({ key: "n", isComposing: true })).toBeNull();

    const field = document.createElement("input");
    expect(actionForKey({ key: "n", target: field })).toBeNull();
    const area = document.createElement("textarea");
    expect(actionForKey({ key: "ArrowUp", target: area })).toBeNull();
  });

  it("means nothing for a key the deck does not use", () => {
    expect(actionForKey({ key: "q" })).toBeNull();
    expect(actionForKey({ key: "Enter" })).toBeNull();
  });
});
