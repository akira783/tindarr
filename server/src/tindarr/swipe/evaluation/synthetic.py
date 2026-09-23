"""The vote set that lives in the repository: invented titles, plausible behaviour.

CI has to run the harness on every pull request, which means the vote set it runs on has
to be public. Real votes are not: a list of what somebody watched and what they thought
of it is exactly the kind of thing a public repository should not carry, whoever says
they do not mind. So the committed fixture is generated, from a seed, and everything in
it is invented — the titles do not exist, and the identifiers are in a range TMDb does
not use.

What is **not** invented is the shape. The vote distribution is the one ADR 0013 was
written from: 47 % of the votes are "I had already seen this", and 63 % of the cards
that were genuinely new were liked. A replay of the engine that produced such a history
must come back out with those two numbers, which is how the harness proves it is not
mis-scoring. The tastes, the popularity skew and the "the famous ones are the ones I
have seen" correlation are modelled on the same measurement.

**No taste profile.** The obvious thing to generate would be the "Loves / Avoids"
bullets of each synthetic person — and they would name the very genres their later votes
were drawn from. A strategy reading them would not be reading a profile, it would be
reading the answer key, and the committed gate would reward it. The fixture therefore
leaves the profile empty, and the gate is profile-blind. A profile written by a real
engine from real votes carries no such leak, so an imported vote set keeps its own.

**What a generated fixture cannot do** is surprise anybody. It has the distribution it
was given, so it says whether the harness computes what it claims to compute; it does
not say whether a strategy will please a real person. Pointing the harness at a real
instance (``tindarr eval import``) is what does that, and that fixture stays private.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from tindarr.swipe.evaluation.dataset import (
    CatalogEntry,
    EvalDataset,
    EvalUser,
    FixtureVote,
    OwnedTitle,
)
from tindarr.swipe.strategy import Novelty, PickKind
from tindarr.swipe.votes import VoteValue

__all__ = ["SYNTHETIC_NAME", "build_synthetic_dataset"]

SYNTHETIC_NAME: Final = "synthetic-99"
_SOURCE: Final = (
    "generated, with the vote distribution ADR 0013 measured (47 % already seen, "
    "63 % liked among the genuinely new)"
)
#: Invented identifiers. TMDb's own ids are nowhere near this range, so a fixture id can
#: never be mistaken for a real title, and nothing here is scraped from anybody's API.
_FIRST_ID: Final = 900_001
_TITLES: Final = 120
_LIBRARY_PER_USER: Final = 5
#: Votes the fork labels 'calibration' before it knows anything about a person.
_CALIBRATION_VOTES: Final = 15
#: Every title above this popularity is "famous" — the pool the already-seen votes are
#: drawn from, because that is where ADR 0013 found them: one card in the 1980s, eight
#: in the 1990s, and the rest among the hits of the last twenty years.
_FAMOUS_POPULARITY: Final = 70.0

_GENRES: Final[tuple[str, ...]] = (
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Fantasy",
    "Horror",
    "Mystery",
    "Romance",
    "Science Fiction",
)
_FRANCHISES: Final[tuple[str, ...]] = (
    "Ardent Sky",
    "Cold Harbour",
    "Lantern Wars",
    "Mirrorline",
    "Salt Road",
    "The Quiet Hours",
)
_ADJECTIVES: Final[tuple[str, ...]] = (
    "Amber",
    "Bright",
    "Crooked",
    "Distant",
    "Eastern",
    "Frozen",
    "Golden",
    "Hidden",
    "Iron",
    "Jagged",
    "Kindred",
    "Lonely",
    "Molten",
    "Northern",
    "Open",
    "Patient",
    "Quiet",
    "Restless",
    "Silver",
    "Tender",
    "Upright",
    "Velvet",
    "Wandering",
    "Yellow",
)
_NOUNS: Final[tuple[str, ...]] = (
    "Anchor",
    "Bridge",
    "Cartographer",
    "Descent",
    "Estuary",
    "Fable",
    "Gardener",
    "Harvest",
    "Inlet",
    "Jetty",
    "Kiln",
    "Lighthouse",
    "Meridian",
    "Notebook",
    "Orchard",
    "Passage",
    "Quarry",
    "Ridge",
    "Signal",
    "Threshold",
    "Undertow",
    "Vessel",
    "Watchman",
    "Zenith",
)


@dataclass(frozen=True, slots=True)
class _UserPlan:
    """One synthetic person: what they like, and how many of each vote they cast."""

    user_id: str
    loves: tuple[str, ...]
    avoids: tuple[str, ...]
    seen_liked: int
    like: int
    dislike: int
    seen_disliked: int
    skip: int
    novelty: Novelty


_USERS: Final[tuple[_UserPlan, ...]] = (
    _UserPlan(
        user_id="user-1",
        loves=("Science Fiction", "Mystery", "Drama"),
        avoids=("Romance", "Horror"),
        seen_liked=16,
        like=11,
        dislike=7,
        seen_disliked=1,
        skip=2,
        novelty="balanced",
    ),
    _UserPlan(
        user_id="user-2",
        loves=("Comedy", "Animation", "Adventure"),
        avoids=("Horror", "Documentary"),
        seen_liked=15,
        like=11,
        dislike=6,
        seen_disliked=0,
        skip=2,
        novelty="familiar",
    ),
    _UserPlan(
        user_id="user-3",
        loves=("Crime", "Documentary", "Drama"),
        avoids=("Fantasy", "Animation"),
        seen_liked=15,
        like=11,
        dislike=6,
        seen_disliked=0,
        skip=2,
        novelty="bold",
    ),
)


def build_synthetic_dataset(seed: int = 20260923) -> EvalDataset:
    """Return the committed fixture, rebuilt from ``seed``.

    Deterministic: the same seed gives the same file, byte for byte, which is what lets
    a test hold the committed copy to the generator rather than to a reviewer's memory.
    """
    draw = random.Random(seed)  # noqa: S311 - a fixture, not a secret
    catalog = _catalog(draw)
    # One household, one shelf: the three of them can have voted on the same title, as
    # three people sharing a media server do.
    users = [_user(plan, catalog, draw) for plan in _USERS]
    return EvalDataset(
        name=SYNTHETIC_NAME,
        source=_SOURCE,
        language="en",
        region="FR",
        catalog=tuple(catalog),
        users=tuple(users),
    )


def _catalog(draw: random.Random) -> list[CatalogEntry]:
    names = sorted({f"{adjective} {noun}" for adjective in _ADJECTIVES for noun in _NOUNS})
    draw.shuffle(names)
    # A long tail under a famous head, shuffled onto the titles: fame and "I have seen
    # it" go together, which is the whole shape ADR 0013 measured.
    fame = [round(100.0 * (1.0 - index / _TITLES) ** 1.2, 3) for index in range(_TITLES)]
    draw.shuffle(fame)
    entries: list[CatalogEntry] = []
    for index in range(_TITLES):
        name = names[index]
        kind = "tv" if index % 10 < 3 else "movie"  # noqa: PLR2004 - three series in ten
        genre_count = 1 + draw.randrange(3)
        genres = tuple(sorted({_GENRES[draw.randrange(len(_GENRES))] for _ in range(genre_count)}))
        franchise = _FRANCHISES[draw.randrange(len(_FRANCHISES))] if index % 4 == 0 else None
        entries.append(
            CatalogEntry(
                tmdb_id=_FIRST_ID + index,
                kind=kind,
                title=name if franchise is None else f"{franchise}: {name}",
                year=1985 + draw.randrange(42),
                genres=genres,
                franchise=franchise,
                popularity=fame[index],
                original_language="en" if draw.randrange(4) else "fr",
                vote_average=round(5.0 + draw.randrange(0, 41) / 10, 1),
                vote_count=50 + draw.randrange(9_950),
                overview=f"{name}, in a few invented words.",
            )
        )
    return entries


def _user(plan: _UserPlan, catalog: Sequence[CatalogEntry], draw: random.Random) -> EvalUser:
    # Nobody votes on the same title twice, so the bookkeeping is per person.
    taken: set[int] = set()
    loves, avoids = set(plan.loves), set(plan.avoids)
    free = list(catalog)
    in_taste = [entry for entry in free if loves & set(entry.genres)]
    off_taste = [entry for entry in free if avoids & set(entry.genres)]
    neutral = [entry for entry in free if entry not in in_taste and entry not in off_taste]
    famous = [entry for entry in in_taste if entry.popularity >= _FAMOUS_POPULARITY]
    quiet = [entry for entry in in_taste if entry.popularity < _FAMOUS_POPULARITY]

    # "Already seen" lands on the famous titles, a plain like on the quieter ones, a
    # dislike outside the taste: the correlation ADR 0013 found, written down.
    recipe: tuple[tuple[VoteValue, tuple[Sequence[CatalogEntry], ...], int], ...] = (
        ("seen_liked", (famous, quiet, free), plan.seen_liked),
        ("like", (quiet, in_taste, free), plan.like),
        ("dislike", (off_taste, neutral, free), plan.dislike),
        ("seen_disliked", (off_taste, neutral, free), plan.seen_disliked),
        ("skip", (neutral, free), plan.skip),
    )
    picks: list[tuple[CatalogEntry, VoteValue]] = []
    for value, pools, count in recipe:
        chosen: VoteValue = value
        picks.extend((entry, chosen) for entry in _draw(pools, count, taken, draw, value))
    draw.shuffle(picks)

    owned = _draw((catalog,), _LIBRARY_PER_USER, taken, draw, "library")
    return EvalUser(
        id=plan.user_id,
        votes=tuple(
            FixtureVote(
                seq=position,
                tmdb_id=entry.tmdb_id,
                kind=entry.kind,
                vote=value,
                pick="calibration" if position < _CALIBRATION_VOTES else _pick_of(value, draw),
            )
            for position, (entry, value) in enumerate(picks)
        ),
        library=tuple(OwnedTitle(tmdb_id=entry.tmdb_id, kind=entry.kind) for entry in owned),
        taste_profile=None,
        novelty=plan.novelty,
    )


def _draw(
    pools: Sequence[Sequence[CatalogEntry]],
    count: int,
    taken: set[int],
    draw: random.Random,
    what: str,
) -> list[CatalogEntry]:
    """Draw ``count`` unused entries, preferring the earlier pools.

    Each tier is shuffled on its own and they are then walked in order, so a vote lands
    on the kind of title it is meant to — a famous one for "already seen", a quieter one
    for a plain like — and the generator still finishes when a tier runs thin.
    """
    available: list[CatalogEntry] = []
    already = set(taken)
    for pool in pools:
        tier = [entry for entry in pool if entry.tmdb_id not in already]
        draw.shuffle(tier)
        available.extend(tier)
        already.update(entry.tmdb_id for entry in tier)
    chosen = available[:count]
    if len(chosen) < count:  # pragma: no cover - the catalogue is sized to prevent it
        raise ValueError(f"the synthetic catalogue ran out of titles for {what}")
    taken.update(entry.tmdb_id for entry in chosen)
    return chosen


def _pick_of(vote: VoteValue, draw: random.Random) -> PickKind:
    """Label a card the way the fork's engine would have, for the record only."""
    if vote in ("seen_liked", "seen_disliked"):
        return "safe"
    return "explore" if draw.randrange(3) == 0 else "safe"
