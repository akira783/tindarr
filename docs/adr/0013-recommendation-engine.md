# 0013. The recommendation engine: retrieval first, the model second

- Status: accepted
- Date: 2026-09-23

## Context

Step 4 was planned as a port of the engine that already runs in the SuggestArr fork this
project comes from ([ADR 0008](0008-extraction-from-suggestarr.md)). Before porting it,
we measured what that engine actually does, on 99 real votes from its author.

| Pick type | Cards | Already seen | Liked, among the genuinely new |
|---|---|---|---|
| Safe | 52 | 28 (54 %) | 18 of 24 |
| Explore | 26 | 6 (23 %) | 10 of 20 |
| Calibration | 21 | 13 (62 %) | 5 of 8 |
| **Total** | **99** | **47 (47 %)** | **33 of 52 (63 %)** |

Two things stand out. The engine reads taste well: when a card is new to the user, it is
liked 63 % of the time, and 26 titles were requested. But **nearly half of every batch is
a title the user already knows**, and it is worst on the "safe" picks, which are the ones
meant to land. The engine conflates "close to your taste" with "famous".

The mechanism explains it. The model is asked for titles from its own memory, those titles
are then looked up on TMDb and the ones that fail the filters are dropped. So it can only
propose what is famous enough to be memorised, it knows nothing of recent releases, and a
batch of 15 suggestions yields about 6 usable cards.

## Decision

**1. Retrieval first, the model second.** TMDb builds the candidate pool — titles similar
to what the user liked, plus filtered discovery by genre, era, country, rating and
popularity. The model's job is to pick from that pool, order it and explain each pick. It
can no longer invent a title that does not exist, it sees what came out last week, the
yield per batch rises and the cost per card falls. The prompt, the safe/explore mix, the
novelty levels, the mood and the taste profile survive from the fork: they were never the
weak part.

**2. An offline harness before the engine.** Past votes are replayed to measure any
candidate strategy — how many cards it would have produced, how many already-seen titles,
how well it agrees with the votes that were cast. No strategy ships without those numbers.

**3. The "already seen" problem is cultural, not technical.** We tested the obvious idea —
import what the user has watched elsewhere — against the same 99 votes, with the author's
real accounts:

| Source | Titles known | Cards it would have avoided |
|---|---|---|
| Netflix viewing history (5 months, one profile) | 70 | 1 |
| Jellyfin watch state | 24 | 0 |
| Seerr requests | 90 | 2 |
| **All three together** | | **3 of 47 (6 %)** |

The already-seen cards are overwhelmingly famous films and series from the 1990s to the
2010s: 1 from the 1980s, 8 from the 1990s, 6 from the 2000s, 22 from the 2010s, 10 from
the 2020s. Nobody's media server knows about a film watched in a cinema fifteen years ago.
Seerr is structurally unable to help, because people request what they have *not* seen.
The Jellyfin figure is low for a second reason worth remembering: a library rebuild (a
debrid setup) took the watch state of the removed items with it.

So the fix is not more history sources:

- **An adaptive popularity floor.** The higher the novelty setting, the lower the
  popularity a candidate is allowed to have. This costs the user nothing and handles the
  bulk of the waste.
- **A calibration grid**, where the user ticks the titles they know among a wall of famous
  posters. Three minutes conveys fifteen years of watching.
- Ratings exports (IMDb, Letterboxd) when the user has them: they carry an exact id *and*
  an opinion.

**4. Watch history is a taste signal, not a filter.** What the imports genuinely bring is
engagement. Counting the episodes watched per series against the total on TMDb reproduces
the "finished / in progress / sampled and dropped" signal the engine already derives from
the media server, over everything watched outside it. On the author's five months of
Netflix history that is 3 series finished, 9 in progress, 30 sampled and dropped — and
those 30 are high-rated series the engine would happily have proposed. No other source
says that.

**5. Requests are shown, not filtered.** A title already in the user's request queue stays
in the deck, marked as such: it is an intention, not a memory.

## Consequences

- Step 4 gains a retrieval layer and an evaluation harness; the prompts and the engine's
  behaviour carry over from the fork.
- TMDb becomes load-bearing: the quality of the discovery filters now decides the quality
  of the deck. Its rate limits and its caching terms apply to the candidate pool.
- File imports (Netflix, IMDb, Letterboxd) belong to step 4 as taste sources. They are
  files the user owns, so they depend on no third-party API policy — unlike Trakt, whose
  API use policy of 2026-09-16 forbids ingesting several users' Trakt data into another
  service's recommendation engine. Trakt sync therefore stays a personal-instance feature
  until its maintainers say otherwise in writing.
- The popularity floor can hide a genuinely obscure favourite. The novelty selector is the
  user's escape hatch, and the calibration grid lets them say what they already know.

## Alternatives considered

- **Port the fork's engine unchanged.** Fastest to a working deck, and it is proven on 99
  votes — but it ships the defect the numbers above measure. Rejected.
- **A vector taste profile (embeddings) for retrieval and diversity.** Finer recall and
  proper batch diversity, but a second representation to keep in sync with the editable
  text profile. Postponed, not dismissed: the harness will say whether the hybrid pool
  already covers it.
- **More history sources (Trakt, per-service GDPR exports).** Measured above at 6 % of the
  problem, for a third-party dependency each. Kept only as taste signals.
