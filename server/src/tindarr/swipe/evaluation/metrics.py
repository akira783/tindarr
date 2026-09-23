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

from tindarr.swipe.evaluation.costs import CostMeter
from tindarr.swipe.evaluation.replay import BatchOutcome, CardStatus, ReplayOptions

__all__ = [
    "DIRECTIONS",
    "KINDS",
    "Counts",
    "EvaluationReport",
    "summarize",
]

#: Which way a metric has to move to be an improvement.
type Direction = Literal["higher", "lower", "flat"]
#: Whether a number was counted or guessed.
type Kind = Literal["measured", "estimated"]

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
    "waste_rate": "lower",
    "coverage": "flat",
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

KINDS: Final[Mapping[str, Kind]] = {
    key: ("estimated" if key == "llm_tokens_per_card" else "measured") for key in DIRECTIONS
}

#: One line each, printed under the table. A metric nobody can explain gets misread.
MEANINGS: Final[Mapping[str, str]] = {
    "fill_rate": "cards returned, over cards asked for",
    "usable_per_batch": "cards per batch that could really have been shown",
    "waste_rate": "proposed cards the strategy had been told to avoid",
    "coverage": "usable cards the fixture has an opinion on (context, not a score)",
    "already_seen_rate": "scored cards the user had already watched (ADR 0013: 47 %)",
    "like_rate": "scored cards the user wanted, and had not seen",
    "new_like_rate": "likes among the cards that were new to them (ADR 0013: 63 %)",
    "agreement": "scored cards the user liked, already seen or not (read with the row above)",
    "skip_rate": "usable cards the user had no opinion on at all",
    "genre_diversity": "distinct genres per catalogued card in a batch",
    "franchise_repeat_rate": "batches serving the same franchise twice",
    "tmdb_calls_per_batch": "metadata calls a batch cost",
    "llm_calls_per_batch": "model calls a batch cost",
    "llm_tokens_per_card": "tokens per usable card",
}

_PERCENT_METRICS: Final[frozenset[str]] = frozenset(
    {
        "fill_rate",
        "waste_rate",
        "coverage",
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
    likes: int = 0
    dislikes: int = 0
    seen_liked: int = 0
    seen_disliked: int = 0
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
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        """Return the canonical JSON text of the report, newline-terminated."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def table(self) -> str:
        """Return the report as the text the harness prints."""
        return render(self)


def summarize(  # noqa: PLR0913, PLR0917 - a report is exactly its inputs
    dataset: str,
    source: str,
    strategy: str,
    batches: Sequence[BatchOutcome],
    meter: CostMeter,
    options: ReplayOptions,
) -> EvaluationReport:
    """Aggregate scored batches into a report. Pure: same batches, same numbers."""
    counts = _counts(batches, meter)
    metrics = _metrics(counts, batches)
    return EvaluationReport(
        dataset=dataset,
        source=source,
        strategy=strategy,
        options=options,
        counts=counts,
        metrics=metrics,
        notes=_notes(counts, metrics),
    )


def _counts(batches: Sequence[BatchOutcome], meter: CostMeter) -> Counts:
    tally: dict[CardStatus, int] = {}
    requested = proposed = usable = catalogued = 0
    for batch in batches:
        requested += batch.requested
        proposed += len(batch.cards)
        catalogued += batch.known
        for card in batch.cards:
            tally[card.status] = tally.get(card.status, 0) + 1
            usable += int(card.usable)
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
        catalogued=catalogued,
        tmdb_calls=meter.metadata_calls,
        llm_calls=meter.llm_calls,
        llm_input_tokens=meter.llm_input_tokens,
        llm_output_tokens=meter.llm_output_tokens,
        llm_calls_estimated=meter.llm_calls_estimated,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    """Return a rate, or ``None`` when there is nothing to divide by.

    ``None`` is printed as "n/a" and compared as "was there, is gone": a metric that
    quietly becomes zero because its denominator emptied would read as a good result.
    """
    return round(numerator / denominator, _ROUNDING) if denominator else None


def _metrics(counts: Counts, batches: Sequence[BatchOutcome]) -> Mapping[str, float | None]:
    wasted = counts.duplicates + counts.repeats + counts.served_again + counts.owned
    spreads = [
        batch.distinct_genres / batch.known
        for batch in batches
        if batch.distinct_genres is not None and batch.known
    ]
    judged = [batch.franchise_repeat for batch in batches if batch.franchise_repeat is not None]
    return {
        "fill_rate": _ratio(counts.proposed, counts.requested),
        "usable_per_batch": _ratio(counts.usable, counts.batches),
        "waste_rate": _ratio(wasted, counts.proposed),
        "coverage": _ratio(counts.scored, counts.usable),
        "already_seen_rate": _ratio(counts.seen_liked + counts.seen_disliked, counts.scored),
        "like_rate": _ratio(counts.likes, counts.scored),
        "new_like_rate": _ratio(counts.likes, counts.likes + counts.dislikes),
        "agreement": _ratio(counts.likes + counts.seen_liked, counts.scored),
        "skip_rate": _ratio(counts.skipped, counts.usable),
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
    if counts.llm_calls_estimated:
        notes.append(
            f"{counts.llm_calls_estimated} of {counts.llm_calls} model calls reported no "
            "token counts; those tokens are estimated at four characters each."
        )
    return tuple(notes)


def _format(key: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    if key in _PERCENT_METRICS:
        return f"{value:.1%}"
    return f"{value:.2f}"


def render(report: EvaluationReport) -> str:
    """Return the table the harness prints, with its legend and its caveats."""
    counts = report.counts
    lines = [
        f"strategy   {report.strategy}",
        f"vote set   {report.dataset}" + (f" ({report.source})" if report.source else ""),
        "options    "
        + ", ".join(f"{name}={value}" for name, value in report.options.as_dict().items()),
        "",
        f"{'metric':<22} {'value':>8}  {'better':<7} {'kind':<9} meaning",
        f"{'-' * 22} {'-' * 8}  {'-' * 7} {'-' * 9} {'-' * 40}",
    ]
    lines.extend(
        f"{key:<22} {_format(key, report.metrics[key]):>8}  "
        f"{DIRECTIONS[key]:<7} {KINDS[key]:<9} {MEANINGS[key]}"
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
        f"cost {counts.tmdb_calls} metadata calls, {counts.llm_calls} model calls, "
        f"{counts.llm_input_tokens + counts.llm_output_tokens} tokens",
        "",
    ]
    lines.extend(f"note  {note}" for note in report.notes)
    return "\n".join(lines) + "\n"
