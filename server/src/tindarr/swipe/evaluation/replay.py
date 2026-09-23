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
- **What stops that is recall.** Before the first batch, the replay counts the titles
  this user liked among the votes it is withholding. That number is a property of the
  vote set, not of what any strategy proposed, so it does not move when a strategy
  reaches outside the fixture — which is what makes it the one score an open candidate
  pool cannot dilute (``tindarr.swipe.evaluation.metrics``).
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
from tindarr.swipe.evaluation.costs import BatchCost, CostMeter
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
    #: Where the card sat in the batch, counting from zero. A strategy returns its cards
    #: best first, so a title the user liked at position 0 is worth more than the same
    #: title at position 9, and ``liked_recall_top`` is computed from this.
    rank: int = 0
    #: Whether the strategy handed back the title details a card is built from, for the
    #: title it says they are for. Without this, "no TMDb call" would be a perfect score
    #: on the cost axis for cards that cannot be rendered.
    complete: bool = False

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
    #: What this batch spent, measured at the ports while it was being proposed.
    cost: BatchCost
    #: Usable cards the catalogue knows, so diversity is computed on a real denominator.
    known: int
    #: Distinct genres across those cards; ``None`` without a catalogue.
    distinct_genres: int | None
    #: Whether two cards of this batch belong to the same franchise; ``None`` when the
    #: catalogue knows fewer than two of them and there is nothing to compare.
    franchise_repeat: bool | None
    #: How many titles this user liked, among the votes the replay withheld at the
    #: start of their run. The same number on every batch of one user: it is the
    #: denominator of recall, and it is deliberately a property of the **vote set**
    #: rather than of what any strategy proposed, so no strategy can shrink it.
    liked_available: int = 0

    @property
    def usable(self) -> tuple[CardOutcome, ...]:
        """The cards that could legitimately have been shown."""
        return tuple(card for card in self.cards if card.usable)


async def replay(
    dataset: EvalDataset,
    factory: StrategyFactory,
    options: ReplayOptions | None = None,
    meter: CostMeter | None = None,
) -> tuple[BatchOutcome, ...]:
    """Walk every user of ``dataset`` past a fresh strategy and return the scored batches.

    A strategy is built per user. That is a convention, not a sandbox: the candidate pool
    and the metered ports a caller builds are usually shared, so a strategy *could* carry
    a conclusion from one person to the next. It cannot reach a vote that has not been
    revealed, which is the property the numbers depend on.

    ``meter`` is read — never reset — after each batch, and the difference is what that
    batch is charged. A total that goes backwards means something wrote to the meter, and
    the replay stops rather than publish a cost it cannot stand behind.
    """
    settings = options or ReplayOptions()
    catalog = dataset.by_ref
    tally = meter or CostMeter()
    batches: list[BatchOutcome] = []
    for position, user in enumerate(sorted(dataset.users, key=lambda row: row.id)):
        batches.extend(
            await _replay_user(dataset, user, position, factory, settings, catalog, tally)
        )
    return tuple(batches)


async def _replay_user(  # noqa: PLR0913, PLR0917 - one call site; splitting it hides the loop
    dataset: EvalDataset,
    user: EvalUser,
    position: int,
    factory: StrategyFactory,
    options: ReplayOptions,
    catalog: Mapping[TitleRef, CatalogEntry],
    meter: CostMeter,
) -> list[BatchOutcome]:
    votes = user.ordered_votes
    if len(votes) <= options.warm_up:
        return []
    strategy = factory()
    # The denominator of recall, fixed before the first batch and never touched again.
    # A vote revealed as history can only come back as waste, so the titles this user
    # liked *within the warm-up* were never findable and are not counted against
    # anybody. Note the ones held back but revealed later are counted: a strategy has
    # only the batches before that reveal to find them, which is the same handicap for
    # every strategy and is what makes the number comparable.
    liked_available = sum(1 for vote in votes[options.warm_up :] if vote.value == "like")
    # What the household owns, kept here. The index handed to a strategy is rebuilt for
    # every batch and scoring never consults it: an object a strategy holds is an object
    # a strategy can empty, and "owned" would then stop being waste.
    owned = frozenset(user.library_index.refs)
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
            library=user.library_index,
            served=frozenset(served),
            batch_index=index,
            seed=options.seed + position * 1_000 + index,
        )
        before = meter.snapshot()
        proposed = await strategy.propose(context, options.batch_size)
        try:
            cost = meter.snapshot().since(before)
        except ValueError as tampered:
            raise ReplayError(str(tampered)) from None
        if len(proposed) > options.batch_size:
            raise ReplayError(
                f"{getattr(strategy, 'name', 'the strategy')} returned "
                f"{len(proposed)} cards for a batch of {options.batch_size}"
            )
        outcomes.append(
            _score(
                _Scoring(user.id, index, options.batch_size, cost, owned, liked_available),
                proposed,
                context,
                future,
                catalog,
            )
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


@dataclass(frozen=True, slots=True)
class _Scoring:
    """What a batch is scored against, gathered by the harness rather than the strategy."""

    user_id: str
    index: int
    requested: int
    cost: BatchCost
    owned: frozenset[TitleRef]
    liked_available: int


def _score(
    scoring: _Scoring,
    proposed: Sequence[Candidate],
    context: StrategyContext,
    future: Mapping[TitleRef, Vote],
    catalog: Mapping[TitleRef, CatalogEntry],
) -> BatchOutcome:
    voted = context.voted
    seen_in_batch: set[TitleRef] = set()
    cards: list[CardOutcome] = []
    for rank, candidate in enumerate(proposed):
        ref = candidate.ref
        status = _status(ref, voted, context.served, scoring.owned, seen_in_batch, future)
        seen_in_batch.add(ref)
        details = candidate.details
        cards.append(
            CardOutcome(
                ref=ref,
                status=status,
                pick=candidate.pick,
                rank=rank,
                complete=details is not None and details.ref == ref,
            )
        )
    spread = _diversity(cards, catalog)
    return BatchOutcome(
        user_id=scoring.user_id,
        index=scoring.index,
        requested=scoring.requested,
        cards=tuple(cards),
        cost=scoring.cost,
        known=spread.known,
        distinct_genres=spread.distinct_genres,
        franchise_repeat=spread.franchise_repeat,
        liked_available=scoring.liked_available,
    )


def _status(  # noqa: PLR0913, PLR0917 - the six facts that decide what a card became
    ref: TitleRef,
    voted: frozenset[TitleRef],
    served: frozenset[TitleRef],
    owned: frozenset[TitleRef],
    seen_in_batch: set[TitleRef],
    future: Mapping[TitleRef, Vote],
) -> CardStatus:
    if ref in seen_in_batch:
        return "duplicate"
    if ref in voted:
        return "repeat"
    if ref in served:
        return "served_again"
    if ref in owned:
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
