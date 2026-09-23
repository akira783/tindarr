"""Walking a vote set past a strategy, one batch at a time, without telling it the end.

The shape of a replay is simple and its honesty is entirely in what it withholds. For
each user, the votes are ordered. A prefix of them is revealed as history; the strategy
is asked for a batch; every card it proposes is looked up in the votes that were **not**
revealed. Then the next slice is revealed and it is asked again.

What that buys, and what it does not:

- A card the user eventually liked counts as a hit. A card they eventually said they had
  already seen counts as waste, which is the number ADR 0013 is about.
- **A card nobody ever voted on counts as nothing.** It is not a hit, not a miss; it is
  reported as the gap in coverage that it is. A strategy proposing obscure titles nobody
  in the fixture ever saw would otherwise look flawless.
- The votes were cast on **another engine's** cards. A candidate strategy proposing a
  title the user never saw cannot be scored, and the user's history does not change in
  response to what the candidate proposed. The replay is therefore a comparison of
  strategies against one fixed history, not a simulation of a user.
- The reveal schedule is fixed — the same prefixes for every strategy — precisely so
  that two strategies are asked the same questions.

Nothing here reads a clock, a database or a random source it was not given a seed for.
Two runs on the same file produce the same numbers, which is what lets CI fail on a
change instead of on the weather.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from tindarr.ports.titles import TitleRef
from tindarr.swipe.evaluation.dataset import CatalogEntry, EvalDataset, EvalUser
from tindarr.swipe.strategy import Candidate, PickKind, StrategyContext, StrategyFactory
from tindarr.swipe.votes import Vote

__all__ = [
    "WASTED_STATUSES",
    "BatchOutcome",
    "CardOutcome",
    "CardStatus",
    "ReplayError",
    "ReplayOptions",
    "replay",
]

#: What became of one proposed card.
#:
#: The first four are defects: the strategy was told about all of them in its context
#: and proposed them anyway. The next five are the user's own words. ``unknown`` is the
#: honest gap: this fixture never asked them.
type CardStatus = Literal[
    "duplicate",
    "repeat",
    "served_again",
    "owned",
    "like",
    "dislike",
    "seen_liked",
    "seen_disliked",
    "skip",
    "unknown",
]

#: Cards that should never have been in the batch. They are not "usable" and they are
#: never scored: counting them as misses would let a strategy improve its rates by
#: repeating itself.
WASTED_STATUSES: frozenset[CardStatus] = frozenset({"duplicate", "repeat", "served_again", "owned"})


class ReplayError(RuntimeError):
    """A strategy broke the rules of the replay."""


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    """How a vote set is walked.

    Part of a report's identity: change any of it and the numbers answer another
    question, which is why the gate refuses to compare two runs walked differently.
    """

    #: Cards asked for per batch. The fork serves ten.
    batch_size: int = 10
    #: Votes revealed before the first batch, so a strategy is not asked to work from
    #: nothing. Below this, a user contributes no batch at all.
    warm_up: int = 10
    #: A cap on batches per user, for a quick run. ``None`` walks the whole history.
    max_batches: int | None = None
    #: The root of every seed a strategy is given.
    seed: int = 1

    def __post_init__(self) -> None:
        """Refuse settings that would make a report meaningless."""
        if self.batch_size < 1:
            raise ValueError("a batch holds at least one card")
        if self.warm_up < 0:
            raise ValueError("the warm-up cannot be negative")
        if self.max_batches is not None and self.max_batches < 1:
            raise ValueError("a run walks at least one batch per user")

    def as_dict(self) -> dict[str, int | None]:
        """Return the options as a report and a baseline file spell them."""
        return {
            "batch_size": self.batch_size,
            "warm_up": self.warm_up,
            "max_batches": self.max_batches,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class CardOutcome:
    """One proposed card and what the fixture says about it."""

    ref: TitleRef
    status: CardStatus
    pick: PickKind

    @property
    def usable(self) -> bool:
        """Whether this card could legitimately have been shown to the user."""
        return self.status not in WASTED_STATUSES


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """One batch, scored."""

    user_id: str
    index: int
    requested: int
    cards: tuple[CardOutcome, ...]
    #: Usable cards the catalogue knows, so diversity is computed on a real denominator.
    known: int
    #: Distinct genres across those cards; ``None`` without a catalogue.
    distinct_genres: int | None
    #: Whether two cards of this batch belong to the same franchise; ``None`` when the
    #: catalogue knows fewer than two of them and there is nothing to compare.
    franchise_repeat: bool | None

    @property
    def usable(self) -> tuple[CardOutcome, ...]:
        """The cards that could legitimately have been shown."""
        return tuple(card for card in self.cards if card.usable)


async def replay(
    dataset: EvalDataset, factory: StrategyFactory, options: ReplayOptions | None = None
) -> tuple[BatchOutcome, ...]:
    """Walk every user of ``dataset`` past a fresh strategy and return the scored batches.

    A strategy is built per user, so nothing one person's run learned can reach another's.
    """
    settings = options or ReplayOptions()
    catalog = dataset.by_ref
    batches: list[BatchOutcome] = []
    for position, user in enumerate(sorted(dataset.users, key=lambda row: row.id)):
        batches.extend(await _replay_user(dataset, user, position, factory, settings, catalog))
    return tuple(batches)


async def _replay_user(  # noqa: PLR0913, PLR0917 - one call site; splitting it hides the loop
    dataset: EvalDataset,
    user: EvalUser,
    position: int,
    factory: StrategyFactory,
    options: ReplayOptions,
    catalog: Mapping[TitleRef, CatalogEntry],
) -> list[BatchOutcome]:
    votes = user.ordered_votes
    if len(votes) <= options.warm_up:
        return []
    strategy = factory()
    library = user.library_index
    served: set[TitleRef] = set()
    outcomes: list[BatchOutcome] = []
    index = 0
    while True:
        revealed = options.warm_up + index * options.batch_size
        if revealed >= len(votes):
            break
        if options.max_batches is not None and index >= options.max_batches:
            break
        history = votes[:revealed]
        # The only place the future exists. It is built here and read only to score
        # what came back; it is never part of a context.
        future = _future(votes[revealed:])
        context = StrategyContext(
            user_id=user.id,
            media_kind=user.wanted_kind,
            novelty=user.novelty_level,
            mood=user.mood,
            language=dataset.language,
            region=dataset.region,
            taste_profile=user.taste_profile,
            history=history,
            library=library,
            served=frozenset(served),
            batch_index=index,
            seed=options.seed + position * 1_000 + index,
        )
        proposed = await strategy.propose(context, options.batch_size)
        if len(proposed) > options.batch_size:
            raise ReplayError(
                f"{getattr(strategy, 'name', 'the strategy')} returned "
                f"{len(proposed)} cards for a batch of {options.batch_size}"
            )
        outcomes.append(
            _score(user.id, index, options.batch_size, proposed, context, future, catalog)
        )
        served.update(card.ref for card in proposed)
        index += 1
    return outcomes


def _future(votes: Sequence[Vote]) -> Mapping[TitleRef, Vote]:
    future: dict[TitleRef, Vote] = {}
    for vote in votes:
        # A user votes once per title; should a fixture hold two, the earlier one is
        # what they would have said at this point.
        future.setdefault(vote.ref, vote)
    return future


def _score(  # noqa: PLR0913, PLR0917 - the scoring inputs, passed rather than captured
    user_id: str,
    index: int,
    requested: int,
    proposed: Sequence[Candidate],
    context: StrategyContext,
    future: Mapping[TitleRef, Vote],
    catalog: Mapping[TitleRef, CatalogEntry],
) -> BatchOutcome:
    voted = context.voted
    seen_in_batch: set[TitleRef] = set()
    cards: list[CardOutcome] = []
    for candidate in proposed:
        ref = candidate.ref
        status = _status(ref, voted, context, seen_in_batch, future)
        seen_in_batch.add(ref)
        cards.append(CardOutcome(ref=ref, status=status, pick=candidate.pick))
    spread = _diversity(cards, catalog)
    return BatchOutcome(
        user_id=user_id,
        index=index,
        requested=requested,
        cards=tuple(cards),
        known=spread.known,
        distinct_genres=spread.distinct_genres,
        franchise_repeat=spread.franchise_repeat,
    )


def _status(
    ref: TitleRef,
    voted: frozenset[TitleRef],
    context: StrategyContext,
    seen_in_batch: set[TitleRef],
    future: Mapping[TitleRef, Vote],
) -> CardStatus:
    if ref in seen_in_batch:
        return "duplicate"
    if ref in voted:
        return "repeat"
    if ref in context.served:
        return "served_again"
    if context.library.owns(ref):
        return "owned"
    vote = future.get(ref)
    return vote.value if vote is not None else "unknown"


@dataclass(frozen=True, slots=True)
class _Spread:
    known: int
    distinct_genres: int | None
    franchise_repeat: bool | None


def _diversity(cards: Sequence[CardOutcome], catalog: Mapping[TitleRef, CatalogEntry]) -> _Spread:
    """Count genres and franchise repeats over the catalogue.

    Never over what the strategy said about its own picks: a metric a strategy can
    write its own answer to measures nothing.
    """
    entries = [
        entry for card in cards if card.usable and (entry := catalog.get(card.ref)) is not None
    ]
    if not entries:
        return _Spread(known=0, distinct_genres=None, franchise_repeat=None)
    genres = {genre for entry in entries for genre in entry.genres}
    franchises = [entry.franchise for entry in entries if entry.franchise]
    repeat = len(franchises) != len(set(franchises)) if len(entries) > 1 else None
    return _Spread(known=len(entries), distinct_genres=len(genres), franchise_repeat=repeat)
