# 0012. Which Plex token Tindarr keeps

- Status: proposed (the owner's decision is pending)
- Date: 2026-09-23
- Amends: [0005](0005-adapters-and-ai-providers.md) (the Plex half of the media server
  adapter), when it is accepted

> This ADR is **not implemented**. Step 3 built the Plex adapter on what the stored
> credential can already do, and wrote down precisely what it cannot. Both options below
> are live; neither has been coded beyond what exists today.

## Context

Step 2 stores one Plex credential: the **account token** of the person who owns the
configured server, collected through a PIN and kept in the media server connector's
secret ([the authentication reference](../auth.md#4-sign-in-through-the-media-server)).
It is what identifies the household to plex.tv and what every Plex call is made with.

An account token is not a server token. It is the credential of a plex.tv **account**,
and anything that account can do, it can do: list and manage every server the account
owns, read and change its settings, see its friends and its sharing, remove devices, and
— in the cases that matter here — read the watch state the server keeps for that one
account.

Step 3 had to read three things from Plex, and only one of them came out whole:

- **The library** (`/library/sections`, `/library/sections/{key}/all?includeGuids=1`).
  Fine: the owner token reads everything the server holds, and the TMDb ids come out of
  the `Guid` list.
- **The owner's own watch state.** Fine: a Plex server serves the view state of
  whichever token asked, so `viewCount`, `viewOffset` and `lastViewedAt` on the library
  listing are the owner's, in full, including what they are part-way through.
- **Everybody else's watch state.** Not fine. Those fields do not exist for another
  account in an answer made with the owner's token. What the owner *can* read is the
  server's history, `/status/sessions/history/all?accountID=<id>`, which is a log of
  completed viewings: what was played, when, by which account. There is no offset in it.

So today, in `tindarr.adapters.plex_library`, a Plex household that is not the owner
gets engagement reconstructed from the history: a film that appears was watched, a
series is counted by how many distinct episodes appear, and the 60 % rule is applied to
that count. Those users can never be reported as watching something *now*, and never as
having abandoned something half-way, because the credential cannot know it — not because
they did not.

Two other things follow from the same credential, and they are why this is a decision
rather than a bug report:

- **The user list.** Tindarr's hourly sync reads who may use the server from plex.tv
  (`/api/users`, the sharing list), with the account token. A server-scoped token cannot
  read it: sharing belongs to the account, not to the server.
- **Per-user tokens.** The account token is also the only way to *obtain* one. plex.tv
  hands out a per-server token for a friend through the owner's sharing endpoints, and
  switches to a Home user with the owner's PIN. A server-scoped token can do neither.

The question is therefore not "which token reads more" — the account token wins that
outright — but **what a leak of the stored secret costs**.

## Decision

Two options. Neither is implemented; this section states them so the owner can choose.

### Option A — keep the account token (what exists today)

The connector keeps storing the owner's plex.tv account token, encrypted with every
other secret.

- Per-user watch progress becomes reachable: with the account token, Tindarr can ask
  plex.tv for each shared user's server token, or switch to a Home user, and then read
  that user's own `viewOffset` and "continue watching" exactly as it does for the owner.
  Plex users would get the same five engagement states as Jellyfin users.
- The user sync keeps working as it does now.
- **A leak reaches the whole plex.tv account**: every server it owns, its settings, its
  sharing, its devices, its libraries. Not one media server — an account.

### Option B — store a server-scoped token only

The connector stores a token that only works against the configured server, and the
account token is used once, at setup, and then discarded.

- A leak reaches that one server. That is the same blast radius as a Jellyfin API key,
  which is what the rest of the security model is written around
  ([the security model](../security.md#assets)).
- The user sync needs another source. The candidates are all worse: the server's own
  `/accounts` endpoint lists accounts that have *played something*, which is not the
  same as "may use this server" and would silently drop a new friend; or the
  administrator maintains the list by hand.
- Per-user progress stays where step 3 left it: history only, no in-progress, no
  abandonment. It cannot be improved later without the account token coming back.
- Obtaining the server-scoped token still needs the account token **once**, at setup.
  So the flow does not get simpler; it gets one more step and one fewer stored secret.

### Recommendation

**Option A, kept honest by three things**, and only if the owner agrees to the trade.

The argument is that the blast radius of option B is smaller but its *likelihood* is
not, and its cost is real and permanent: a Plex household would keep worse
recommendations for ever, and the user sync — which is what disables somebody who lost
access — would become guesswork. Tindarr already holds a Jellyfin administrator API key
and a Seerr admin key with the same encryption and the same threat model; the account
token is worse in degree, not in kind.

What has to come with it:

1. **Say so in the console, where the token is collected**, in the words above: this is
   an account token, and Tindarr keeps it. Not in a document nobody reads.
2. **Never widen its use.** It is for the library, the watch state and the user sync.
   Nothing in Tindarr manages a Plex account.
3. **Revisit if plex.tv ever offers a scoped credential** that can read sharing. That
   would make option B free, and it would then be the obvious answer.

If the owner prefers option B, the honest version of that choice is: per-user
recommendations on Plex stay at "watched or not", and the user list is maintained by
the administrator. That is a defensible product, and it should be stated as a product
decision rather than discovered later as a missing feature.

## Consequences

**If A is accepted.** The stored secret keeps the asset table's current entry ("Media
server admin API key / Plex owner token — full control of the media server") but that
wording understates it and must be corrected to say "full control of the plex.tv
account". Step 4 may then add per-user tokens, fetched on demand and never stored, and
Plex users get the same engagement fidelity as everyone else.

**If B is accepted.** `docs/auth.md` section 4 and the connector change: the PIN yields
an account token that is used once to mint a server token and is then dropped, the
`media_server_api_key` setting holds the server token, and `PlexTv.shared_users`
disappears from the hourly sync. `tindarr.adapters.plex_library` loses nothing: it
already works from the history for everyone but the owner, and it would then work that
way for the owner too.

**Either way**, one thing does not change and is worth writing down: Plex's history
records viewings, and Tindarr's engagement rules deliberately do not count them. A
title watched nine times is watched, not loved (see
`tindarr.ports.media_server.Engagement`). The history is used to answer "did this
happen?", never "how often?".

**Two assumptions in the history path that whoever implements this should check.**
They are the adapter's today, not the decision's, but they are invisible from the
outside and both fail quietly rather than loudly.

- `tindarr.adapters.plex.PlexServer._history` filters with
  `accountID=<the plex.tv account id>`. That is the id Tindarr keys users by
  ([the authentication reference](../auth.md#4-sign-in-through-the-media-server)), and
  it is the right one for a shared account. **Plex Home / managed users are numbered
  differently**, and a mismatch does not fail: the server returns an empty container,
  and that household member simply appears never to have watched anything. If option A
  is accepted, per-user tokens make this moot; if option B is, it has to be confirmed
  against a real Home setup.
- The history is read `viewedAt:desc` and capped at `HISTORY_LIMIT` (5 000 viewings, ten
  pages). A household past that loses its oldest rows first, which undercounts a long
  series somebody finished years ago and can tip it from "watched" to "abandoned". The
  cap exists so one user's read cannot be unbounded; raising it is cheap, and paging by
  date rather than by count would be better still.

## Alternatives considered

- **Ask each user for their own Plex token.** It is the cleanest credential — each user
  grants exactly their own access — and it is unusable: a household member who has to
  find their `X-Plex-Token` in a browser's developer tools will not, and the ones who
  do will paste an account token of their own, which is the same problem multiplied.
- **Read progress from the Plex server's `/accounts` and an `accountID` filter on the
  library listing.** The filter is honoured on the history endpoint, which is what step 3
  uses. On a library listing it is not documented to swap the view state the answer
  carries, and the view state a Plex server serves is the asking token's. Whoever
  implements the accepted option should confirm that against a real server before
  relying on it either way; it was not verified here, because verifying it needs a Plex
  server with two accounts.
- **Do without per-user progress on Plex entirely** and use the household's aggregate.
  It would make two people in one house see the same deck, which is the feature.
  Rejected.
