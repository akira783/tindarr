# 0008. Extracting the engine from the SuggestArr fork

- Status: accepted
- Date: 2026-09-22

## Context

The feature lives in `akira783/SuggestArr` (branch `feature/discover`, 16 files on the
server side, about 7,000 lines including tests and UI). It is deployed and used. The
fork must stay untouched until the app replaces it.

## Decision

- **Copy, not subtree.** The files that are needed are copied into `server/`, each
  with a header saying it is derived from SuggestArr (MIT) and from which commit.
  `NOTICE.md` carries the original copyright.
- **Tests first.** The existing tests are ported as characterization tests before
  any refactoring. They are then moved onto the ports.
- **Clients.** Only the parts of the SuggestArr clients that Swipe uses are kept:
  engagement, TMDb search/filters/providers/trailers, OMDb ratings, Seerr request,
  translation. They are rewritten into adapters.
- **Hacks removed.** For example `AiSearchService.__new__` used to reach a TMDb client.
- **Migration path.** An optional one-shot command, `tindeerr import suggestarr`, copies
  votes, profiles and preferences from a SuggestArr database file (read-only),
  matching users by media server user id.

## Consequences

- Tindeerr no longer depends on SuggestArr releases. Engine fixes made in one can be
  ported to the other by hand, as long as both exist.
- Fork users keep their history when they switch.
