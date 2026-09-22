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
  Cards expire 24 h after being served, unless they were voted on.
- Votes reference `card_id`. The server copies the title data from its own card row.
  Requests reference a title the user was served or voted on
  ([security](../security.md)).
- Returning a batch marks its cards as served, which feeds the "already shown" list of
  the next prompt.

## Consequences

- Clients cannot forge card contents or pick types, and statistics stay trustworthy.
- A batch prepared by the warm-up is still there after a restart, so no generation is
  paid twice.
- A little more storage, which a periodic purge of unvoted expired cards keeps small.
