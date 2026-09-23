"""Turning scored batches into numbers, and saying what each number is worth.

Every rate here has a denominator that is printed next to it. That is not decoration:
the easiest way to look good on a replay is to propose three cards instead of ten, so a
like rate without the count of cards it was computed over is a number that can be gamed.
The counts are part of the report, and the CI gate reads them as well.

Each metric declares two things:

- **which way is better**, so the gate knows whether a rise is a win or a regression;
- **whether it is measured or estimated.** Everything counted against the fixture is
  measured. Only the token counts of a provider that reports none are estimated, and the
  report says how many calls that was.

None of this says a strategy is *good*. It says what it would have produced against one
recorded history. The limits are written down in docs/evaluation.md and repeated in the
report's own notes, because a number printed without them will eventually be quoted
without them.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Final, Literal

from tindarr.swipe.evaluation.costs import BatchCost
from tindarr.swipe.evaluation.replay import BatchOutcome, CardStatus, ReplayOptions

__all__ = [
    "BASIS",
    "COUNT_OF_BASIS",
    "DIRECTIONS",
    "MEANINGFUL_BASIS",
    "Counts",
    "EvaluationReport",
    "comparable",
    "summarize",
]

#: Which way a metric has to move to be an improvement.
type Direction = Literal["higher", "lower", "flat"]
#: Whether a number was counted or guessed.
type Kind = Literal["measured", "estimated"]
#: What a number needs in order to mean anything.
#:
#: ``pool-free`` survives an open candidate pool: it is computed over what the strategy
#: was asked for, or over a denominator the vote set fixes before the run. ``votes``
#: divides by the proposed cards this fixture happens to have an opinion on, and
#: ``catalogue`` by the proposed cards it can describe — both collapse the moment a
#: strategy retrieves from the whole of TMDb instead of from the fixture's own titles.
type Basis = Literal["pool-free", "votes", "catalogue"]

#: Read this as the harness's opinion, stated once.
#:
#: ``flat`` means "print it, do not grade it", and two metrics are deliberately there.
#: **Coverage** measures how much of a strategy's output this fixture happens to have an
#: opinion on; a strategy that reaches past the recorded history lowers it, and that is
#: not a fault. **Agreement** counts ``seen_liked`` as a hit, which it is — the user did
#: like the film — so the strategy ADR 0013 asks for, the one that stops serving titles
#: people have already watched, will *lower* it. Grading either would make the gate
#: punish the improvement it exists to protect. The denominators are guarded by the
#: counts instead (``tindarr.swipe.evaluation.gate``).
DIRECTIONS: Final[Mapping[str, Direction]] = {
    "fill_rate": "higher",
    "usable_per_batch": "higher",
    "complete_rate": "higher",
    "waste_rate": "lower",
    "liked_recall": "higher",
    "liked_recall_top": "higher",
    "seen_per_batch": "lower",
    "disliked_per_batch": "lower",
    "coverage": "flat",
    "catalogue_coverage": "flat",
    "already_seen_rate": "lower",
    "like_rate": "higher",
    "new_like_rate": "higher",
    "agreement": "flat",
    "skip_rate": "lower",
    "genre_diversity": "higher",
    "franchise_repeat_rate": "lower",
    "tmdb_calls_per_batch": "lower",
    "llm_calls_per_batch": "lower",
    "llm_tokens_per_card": "lower",
}

#: What each number needs to be worth reading, printed as a column so nobody has to
#: remember it.
#:
#: Four metrics are the answer to an **open candidate pool**, and they are all
#: ``pool-free``. ``liked_recall`` and ``liked_recall_top`` divide by the titles this
#: user liked among the withheld votes — a denominator the vote set fixes before the run
#: — so a strategy that reaches past the fixture does not shrink them; it just fails to
#: find anything. ``seen_per_batch`` and ``disliked_per_batch`` are counts, not rates:
#: serving a title the user had already watched, or one they turned down, is a fault
#: that is worth the same whether the other nine cards were scoreable or not.
#:
#: They also hold each other up. Recall alone is beaten by proposing everything in
#: sight; the avoidance counts alone are beaten by proposing obscure titles nobody has
#: an opinion on — which scores zero recall. Neither can be gamed without the other
#: saying so, and ``fill_rate`` and ``usable_per_batch`` stop a strategy shrinking the
#: batch to make the counts small.
BASIS: Final[Mapping[str, Basis]] = {
    "fill_rate": "pool-free",
    "usable_per_batch": "pool-free",
    "complete_rate": "pool-free",
    "waste_rate": "pool-free",
    "liked_recall": "pool-free",
    "liked_recall_top": "pool-free",
    "seen_per_batch": "pool-free",
    "disliked_per_batch": "pool-free",
    "coverage": "pool-free",
    "catalogue_coverage": "pool-free",
    "already_seen_rate": "votes",
    "like_rate": "votes",
    "new_like_rate": "votes",
    "agreement": "votes",
    "skip_rate": "votes",
    "genre_diversity": "catalogue",
    "franchise_repeat_rate": "catalogue",
    "tmdb_calls_per_batch": "pool-free",
    "llm_calls_per_batch": "pool-free",
    "llm_tokens_per_card": "pool-free",
}

#: How many cards a rate needs behind it before one strategy's value may be held
#: against another's.
#:
#: A **count**, not a share, because the share is not what makes a percentage a fiction:
#: below a dozen cards one card is worth more than eight points, and the gate's
#: tolerance is two. The number is still printed — it is what happened — and the gate
#: says out loud that it did not compare it (``tindarr.swipe.evaluation.gate``).
#:
#: Shrinking the denominator to escape a comparison does not work, and that is by
#: design: the metrics that go on being graded are the pool-free ones, and a strategy
#: proposing titles nobody voted on scores nothing on ``liked_recall`` while
#: ``seen_per_batch`` keeps counting the cards it wasted.
MEANINGFUL_BASIS: Final = 12
#: How far down a batch a card still counts as a top pick. A deck is answered from the
#: front, so recall in the first three cards is a different claim from recall anywhere.
TOP_RANKS: Final = 3

#: Which count each basis divides by, and which a baseline therefore has to carry.
COUNT_OF_BASIS: Final[Mapping[Basis, str]] = {
    "votes": "scored",
    "catalogue": "catalogued",
}


def comparable(metric: str, counts: Mapping[str, int]) -> bool:
    """Whether ``metric`` rests on enough cards to be held against another run.

    Pool-free metrics always are. The rest need ``MEANINGFUL_BASIS`` cards behind them;
    a run whose coverage collapsed — which is what an open candidate pool does — keeps
    printing them and stops being graded on them.
    """
    needed = COUNT_OF_BASIS.get(BASIS.get(metric, "pool-free"))
    return needed is None or counts.get(needed, 0) >= MEANINGFUL_BASIS


#: The one metric that can be a guess. It is only *reported* as one when a provider
#: actually withheld its token counts, so a fully measured run says "measured".
ESTIMATED_WHEN_UNREPORTED: Final = "llm_tokens_per_card"

#: One line each, printed under the table. A metric nobody can explain gets misread.
MEANINGS: Final[Mapping[str, str]] = {
    "fill_rate": "cards returned, over cards asked for",
    "usable_per_batch": "cards per batch that could really have been shown",
    "complete_rate": "usable cards the strategy handed back with their details",
    "waste_rate": "proposed cards the strategy had been told to avoid",
    "liked_recall": "titles the user liked that the strategy found at all",
    "liked_recall_top": f"the same, counting only the first {TOP_RANKS} cards of a batch",
    "seen_per_batch": "cards per batch the user had already watched (a fault, counted)",
    "disliked_per_batch": "cards per batch the user turned down (a fault, counted)",
    "coverage": "usable cards the fixture has an opinion on (context, not a score)",
    "catalogue_coverage": "usable cards the fixture can describe (context, not a score)",
    "already_seen_rate": "scored cards the user had already watched (ADR 0013: 47 %)",
    "like_rate": "scored cards the user wanted, and had not seen",
    "new_like_rate": "likes among the cards that were new to them (ADR 0013: 63 %)",
    "agreement": "scored cards the user liked, already seen or not (read with the row above)",
    "skip_rate": "judged cards the user had no opinion on at all",
    "genre_diversity": "distinct genres per card a batch was asked for",
    "franchise_repeat_rate": "batches serving the same franchise twice",
    "tmdb_calls_per_batch": "metadata calls a batch cost",
    "llm_calls_per_batch": "model calls a batch cost",
    "llm_tokens_per_card": "tokens per usable card",
}

_PERCENT_METRICS: Final[frozenset[str]] = frozenset(
    {
        "complete_rate",
        "fill_rate",
        "waste_rate",
        "liked_recall",
        "liked_recall_top",
        "coverage",
        "catalogue_coverage",
        "already_seen_rate",
        "like_rate",
        "new_like_rate",
        "agreement",
        "skip_rate",
        "franchise_repeat_rate",
    }
)
#: Below this, the rates rest on too few scored cards to argue with.
_WEAK_COVERAGE: Final = 0.5
_ROUNDING: Final = 6


@dataclass(frozen=True, slots=True)
class Counts:
    """The denominators. Every rate above is one of these over another."""

    users: int = 0
    batches: int = 0
    requested: int = 0
    proposed: int = 0
    duplicates: int = 0
    repeats: int = 0
    served_again: int = 0
    owned: int = 0
    usable: int = 0
    scored: int = 0
    skipped: int = 0
    unknown: int = 0
    #: Usable cards that came with the details a card is rendered from.
    complete: int = 0
    likes: int = 0
    dislikes: int = 0
    seen_liked: int = 0
    seen_disliked: int = 0
    #: Titles the users liked among the votes the replay withheld: the denominator of
    #: recall, fixed by the vote set before any strategy ran.
    liked_available: int = 0
    #: Of those, the ones found in the first ``TOP_RANKS`` cards of a batch.
    likes_in_top: int = 0
    catalogued: int = 0
    tmdb_calls: int = 0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls_estimated: int = 0


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """What one strategy did to one vote set, ready to print, dump or compare."""

    dataset: str
    source: str
    strategy: str
    options: ReplayOptions
    counts: Counts
    metrics: Mapping[str, float | None]
    notes: tuple[str, ...]
    #: Liked titles found in each batch position of a run, batch 0 first, summed over
    #: the users. Printed, never graded: it says whether a strategy finds what the user
    #: wanted early or only once it has exhausted the obvious.
    liked_by_batch: tuple[int, ...] = ()

    @property
    def basis_counts(self) -> Mapping[str, int]:
        """The denominators the basis column depends on, as the gate reads them."""
        return {"scored": self.counts.scored, "catalogued": self.counts.catalogued}

    @property
    def weak(self) -> frozenset[str]:
        """The metrics this run does not support well enough to be compared on."""
        return frozenset(key for key in DIRECTIONS if not comparable(key, self.basis_counts))

    @property
    def kinds(self) -> Mapping[str, Kind]:
        """Whether each number was counted or guessed, for this run."""
        estimated = bool(self.counts.llm_calls_estimated)
        return {
            key: ("estimated" if key == ESTIMATED_WHEN_UNREPORTED and estimated else "measured")
            for key in DIRECTIONS
        }

    def as_dict(self) -> dict[str, object]:
        """Return the report as the JSON document a baseline file holds."""
        return {
            "version": 1,
            "dataset": self.dataset,
            "source": self.source,
            "strategy": self.strategy,
            "options": self.options.as_dict(),
            "counts": asdict(self.counts),
            "metrics": dict(sorted(self.metrics.items())),
            "liked_by_batch": list(self.liked_by_batch),
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        """Return the canonical JSON text of the report, newline-terminated."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def table(self) -> str:
        """Return the report as the text the harness prints."""
        return render(self)


def summarize(
    dataset: str,
    source: str,
    strategy: str,
    batches: Sequence[BatchOutcome],
    options: ReplayOptions,
) -> EvaluationReport:
    """Aggregate scored batches into a report. Pure: same batches, same numbers.

    The cost comes from the batches, each of which was charged the difference between
    two readings of the meter while it was being proposed — not from the meter's final
    state, which is an object the strategy was holding.
    """
    counts = _counts(batches)
    metrics = _metrics(counts, batches)
    return EvaluationReport(
        dataset=dataset,
        source=source,
        strategy=strategy,
        options=options,
        counts=counts,
        metrics=metrics,
        notes=_notes(counts, metrics),
        liked_by_batch=_liked_by_batch(batches),
    )


def _liked_by_batch(batches: Sequence[BatchOutcome]) -> tuple[int, ...]:
    """Liked titles found per batch position, summed over the users."""
    if not batches:
        return ()
    found = [0] * (max(batch.index for batch in batches) + 1)
    for batch in batches:
        found[batch.index] += sum(1 for card in batch.cards if card.status == "like")
    return tuple(found)


def _counts(batches: Sequence[BatchOutcome]) -> Counts:
    tally: dict[CardStatus, int] = {}
    requested = proposed = usable = catalogued = complete = likes_in_top = 0
    spent = BatchCost()
    # One entry per user, not per batch: every batch of a user carries the same number.
    available: dict[str, int] = {}
    for batch in batches:
        requested += batch.requested
        proposed += len(batch.cards)
        catalogued += batch.known
        spent = spent.plus(batch.cost)
        available[batch.user_id] = batch.liked_available
        for card in batch.cards:
            tally[card.status] = tally.get(card.status, 0) + 1
            usable += int(card.usable)
            complete += int(card.usable and card.complete)
            likes_in_top += int(card.status == "like" and card.rank < TOP_RANKS)
    likes, dislikes = tally.get("like", 0), tally.get("dislike", 0)
    seen_liked, seen_disliked = tally.get("seen_liked", 0), tally.get("seen_disliked", 0)
    return Counts(
        users=len({batch.user_id for batch in batches}),
        batches=len(batches),
        requested=requested,
        proposed=proposed,
        duplicates=tally.get("duplicate", 0),
        repeats=tally.get("repeat", 0),
        served_again=tally.get("served_again", 0),
        owned=tally.get("owned", 0),
        usable=usable,
        scored=likes + dislikes + seen_liked + seen_disliked,
        skipped=tally.get("skip", 0),
        unknown=tally.get("unknown", 0),
        likes=likes,
        dislikes=dislikes,
        seen_liked=seen_liked,
        seen_disliked=seen_disliked,
        liked_available=sum(available.values()),
        likes_in_top=likes_in_top,
        catalogued=catalogued,
        complete=complete,
        tmdb_calls=spent.metadata_calls,
        llm_calls=spent.llm_calls,
        llm_input_tokens=spent.llm_input_tokens,
        llm_output_tokens=spent.llm_output_tokens,
        llm_calls_estimated=spent.llm_calls_estimated,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    """Return a rate, or ``None`` when there is nothing to divide by.

    ``None`` is printed as "n/a" and compared as "was there, is gone": a metric that
    quietly becomes zero because its denominator emptied would read as a good result.
    """
    return round(numerator / denominator, _ROUNDING) if denominator else None


def _metrics(counts: Counts, batches: Sequence[BatchOutcome]) -> Mapping[str, float | None]:
    wasted = counts.duplicates + counts.repeats + counts.served_again + counts.owned
    # Divided by the cards the batch was *asked* for, not by the ones the catalogue
    # happens to know about: three cards of three genres must not out-diversify ten
    # cards of ten, and a card the catalogue cannot place contributes no genre.
    spreads = [
        batch.distinct_genres / batch.requested
        for batch in batches
        if batch.distinct_genres is not None and batch.known and batch.requested
    ]
    judged = [batch.franchise_repeat for batch in batches if batch.franchise_repeat is not None]
    return {
        "fill_rate": _ratio(counts.proposed, counts.requested),
        "usable_per_batch": _ratio(counts.usable, counts.batches),
        "complete_rate": _ratio(counts.complete, counts.usable),
        "waste_rate": _ratio(wasted, counts.proposed),
        # The denominator is the vote set's, not the run's: it is the same number for
        # every strategy walked with these options, which is what lets two strategies
        # drawing from different pools be compared at all.
        "liked_recall": _ratio(counts.likes, counts.liked_available),
        "liked_recall_top": _ratio(counts.likes_in_top, counts.liked_available),
        # Counted, not rated. A card the user had already watched is one wasted swipe
        # whether or not the nine beside it happened to be scoreable, and dividing it by
        # a denominator that an open pool empties is how that fault disappears.
        "seen_per_batch": _ratio(counts.seen_liked + counts.seen_disliked, counts.batches),
        "disliked_per_batch": _ratio(counts.dislikes, counts.batches),
        "coverage": _ratio(counts.scored, counts.usable),
        "catalogue_coverage": _ratio(counts.catalogued, counts.usable),
        "already_seen_rate": _ratio(counts.seen_liked + counts.seen_disliked, counts.scored),
        "like_rate": _ratio(counts.likes, counts.scored),
        "new_like_rate": _ratio(counts.likes, counts.likes + counts.dislikes),
        "agreement": _ratio(counts.likes + counts.seen_liked, counts.scored),
        # Over the cards the fixture has an opinion on, not over every usable card: a
        # denominator padded with titles nobody voted on would drive this to zero.
        "skip_rate": _ratio(counts.skipped, counts.scored + counts.skipped),
        "genre_diversity": round(sum(spreads) / len(spreads), _ROUNDING) if spreads else None,
        "franchise_repeat_rate": _ratio(sum(judged), len(judged)),
        "tmdb_calls_per_batch": _ratio(counts.tmdb_calls, counts.batches),
        "llm_calls_per_batch": _ratio(counts.llm_calls, counts.batches),
        "llm_tokens_per_card": _ratio(
            counts.llm_input_tokens + counts.llm_output_tokens, counts.usable
        ),
    }


def _notes(counts: Counts, metrics: Mapping[str, float | None]) -> tuple[str, ...]:
    notes = [
        "The votes were cast on another engine's cards. A title this strategy proposed "
        "and that engine never showed has no vote, so it is counted in neither column: "
        "see 'coverage'.",
        "'Already seen' is what the user declared with an up or down swipe. Nothing here "
        "infers it from a watch history.",
    ]
    if not counts.liked_available:
        notes.append(
            "This run withheld no 'like' vote at all, so recall has no denominator and "
            "says nothing."
        )
    coverage = metrics["coverage"]
    if coverage is None or coverage == 0:
        notes.append("No usable card carries a vote: every rate below rests on nothing.")
    elif coverage < _WEAK_COVERAGE:
        notes.append(
            f"Only {coverage:.0%} of the usable cards carry a vote. The rates below are "
            "weakly supported; read them next to 'usable_per_batch', not alone."
        )
    if not counts.catalogued:
        notes.append("The vote set carries no catalogue, so the diversity metrics are unavailable.")
    basis = {"scored": counts.scored, "catalogued": counts.catalogued}
    weak = sorted(key for key in DIRECTIONS if not comparable(key, basis))
    if weak:
        notes.append(
            f"Marked '?': {', '.join(weak)}. Fewer than {MEANINGFUL_BASIS} cards went into "
            "each, so they are printed and not compared with another strategy's — which is "
            "what an open candidate pool does to every rate. The 'pool-free' rows are the "
            "ones that still carry the comparison."
        )
    if counts.llm_calls_estimated:
        notes.append(
            f"{counts.llm_calls_estimated} of {counts.llm_calls} model calls reported no "
            "token counts; those tokens are estimated at four characters each."
        )
    return tuple(notes)


def _format(key: str, value: float | None, *, weak: bool = False) -> str:
    if value is None:
        return "n/a"
    shown = f"{value:.1%}" if key in _PERCENT_METRICS else f"{value:.2f}"
    return f"{shown}?" if weak else shown


def render(report: EvaluationReport) -> str:
    """Return the table the harness prints, with its legend and its caveats."""
    counts = report.counts
    lines = [
        f"strategy   {report.strategy}",
        f"vote set   {report.dataset}" + (f" ({report.source})" if report.source else ""),
        "options    "
        + ", ".join(f"{name}={value}" for name, value in report.options.as_dict().items()),
        "",
        f"{'metric':<22} {'value':>9}  {'better':<7} {'basis':<9} {'kind':<9} meaning",
        f"{'-' * 22} {'-' * 9}  {'-' * 7} {'-' * 9} {'-' * 9} {'-' * 40}",
    ]
    kinds = report.kinds
    weak = report.weak
    lines.extend(
        f"{key:<22} {_format(key, report.metrics[key], weak=key in weak):>9}  "
        f"{DIRECTIONS[key]:<7} {BASIS[key]:<9} {kinds[key]:<9} {MEANINGS[key]}"
        for key in DIRECTIONS
    )
    lines += [
        "",
        f"{counts.users} users, {counts.batches} batches, "
        f"{counts.proposed}/{counts.requested} cards proposed",
        f"usable {counts.usable} "
        f"(wasted {counts.duplicates + counts.repeats + counts.served_again + counts.owned}: "
        f"{counts.duplicates} duplicate, {counts.repeats} already voted, "
        f"{counts.served_again} already served, {counts.owned} owned)",
        f"scored {counts.scored} "
        f"({counts.likes} like, {counts.dislikes} dislike, "
        f"{counts.seen_liked} seen+liked, {counts.seen_disliked} seen+disliked), "
        f"skipped {counts.skipped}, no vote {counts.unknown}",
        f"found {counts.likes} of the {counts.liked_available} titles these users liked "
        f"({counts.likes_in_top} in the first {TOP_RANKS} cards of a batch); "
        f"by batch: {', '.join(str(found) for found in report.liked_by_batch) or 'none'}",
        f"cost {counts.tmdb_calls} metadata calls, {counts.llm_calls} model calls, "
        f"{counts.llm_input_tokens + counts.llm_output_tokens} tokens",
        "",
    ]
    lines.extend(f"note  {note}" for note in report.notes)
    return "\n".join(lines) + "\n"
