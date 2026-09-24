# 0007. Batches and cards are stored on the server

- Status: accepted
- Date: 2026-09-22

## Context

In the SuggestArr fork, batches live in an in-memory store (lost on restart), and a
vote or request sends back the whole card as the client holds it: title, genres,
poster, pick type. The server then trusts that client data.

## Decision

- A generated batch is stored (`batches`), and so is each card (`cards`), with an
  opaque id, the owner, the TMDb reference, the enriched metadata and the pick type.
  Cards leave the deck 24 h after being served. Votes on them are still accepted
  after that, so a vote queued offline is not lost; unvoted cards are purged 30 days
  after being served.
- Votes reference `card_id`. The server copies the title data from its own card row.
  Undo works on the title (`media_type` + `tmdb_id`). Requests reference a title the
  user was served or voted on
  ([security](../security.md)).
- Returning a batch marks its cards as served, which feeds the "already shown" list of
  the next prompt.

## Consequences

- Clients cannot forge card contents or pick types, and statistics stay trustworthy.
- A batch prepared by the warm-up is still there after a restart, so no generation is
  paid twice.
- A little more storage, which the periodic purge of old unvoted cards keeps small.

## What shipping it added (step 4.5)

Two details the decision left open, settled while implementing it:

- **The 24 h and the 30 days are different clocks on the same column.** `served_at`
  starts both: the card leaves the deck after the first, and is deleted after the second
  if nobody ever answered it. A card that *was* voted on is never deleted, because the
  vote points at it and the pick type on that row is what a statistic is counted from.
- **Idempotency needed a third table.** "Votes reference `card_id`" says nothing about a
  queue re-sent after an undo. `vote_receipts` remembers every `client_vote_id` for 90
  days, independently of the vote it produced, so a phone that reconnects twice cannot
  resurrect an answer the user has since taken back.
