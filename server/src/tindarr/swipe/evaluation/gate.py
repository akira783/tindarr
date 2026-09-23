"""The CI gate: a committed baseline, a tolerance, and a build that fails on a slide.

ADR 0013 says no strategy ships without numbers. A number nobody compares is a number
nobody reads, so the harness's numbers on the committed vote set are themselves
committed, and a run that comes out worse than them by more than a stated margin fails
the build.

Five things make the gate hard to walk past:

- **The run has to be the same run.** A baseline records the vote set, the strategy and
  the replay options. Comparing a report walked with a different batch size, or produced
  by another strategy, is refused rather than quietly allowed.
- **The counts are gated too, and tightly.** The cheapest way to improve every rate is
  to propose fewer cards: three confident picks instead of ten score beautifully. So
  ``batches``, ``usable`` and ``scored`` may not fall at all beyond a small margin,
  whatever the rates do.
- **A metric that disappears is a regression.** A rate that becomes ``n/a`` because its
  denominator emptied is not an improvement, and neither is one that is simply missing
  from the report.
- **A comparison that would be noise is refused, out loud.** A rate divided by the
  proposed cards the fixture happens to have an opinion on collapses the moment a
  strategy retrieves from the whole of TMDb rather than from the fixture's own titles.
  With fewer than ``MEANINGFUL_BASIS`` cards behind it, such a metric is printed and not
  compared, and ``uncomparable`` names every comparison that was skipped — a gate nobody
  knows is off is worse than no gate. What still carries the comparison is the pool-free
  half of the table: recall of what the user liked, the fill and waste rates, and
  whether the cards can be rendered at all.
- **A new strategy does not write its own floor.** Its first baseline would otherwise be
  whatever it happened to score, so a strategy worse than doing nothing clever could
  certify itself. ``check_floor`` holds any strategy that is not one of the reference
  floors to the committed numbers of **one** of them, whole: it has to be no worse than
  at least one floor on every graded metric that run supports. Not the best value of
  each metric across the floors — that composite is a strategy that does not exist, and
  neither floor clears it either.

**The tolerances come from the code, not from the baseline file.** They are written into
the file so a reader can see them, and ignored when checking: a gate whose thresholds
live inside the thing it guards is switched off by a one-token diff that looks like a
number rather than like a policy.

Updating a baseline is meant to be a deliberate act with a diff: ``tindarr eval run
--update-baseline`` regenerates it, the diff shows every number that moved, and the
reason belongs in the commit message.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from tindarr.swipe.evaluation.metrics import (
    BASIS_COUNT,
    BASIS_COUNTS,
    DIRECTIONS,
    MEANINGFUL_BASIS,
    EvaluationReport,
    comparable,
)

__all__ = [
    "FLOOR_EXEMPT",
    "FLOOR_STRATEGIES",
    "GATED_COUNTS",
    "RECORDED_COUNTS",
    "Baseline",
    "BaselineError",
    "Regression",
    "check",
    "check_floor",
    "uncomparable",
]

#: How far a metric may slide before the build fails. Rates are fractions, so 0.02 is
#: two points of percentage; the call counts are per batch, where a whole extra call is
#: the thing worth noticing.
DEFAULT_TOLERANCE: Final = 0.02
DEFAULT_TOLERANCES: Final[Mapping[str, float]] = {
    "usable_per_batch": 0.25,
    "genre_diversity": 0.05,
    "tmdb_calls_per_batch": 1.0,
    "llm_calls_per_batch": 0.25,
    "llm_tokens_per_card": 50.0,
}
#: The strategies whose committed numbers *are* the floor. They are compared with
#: themselves and with nothing else; everything else has to clear them.
FLOOR_STRATEGIES: Final[tuple[str, ...]] = ("popular", "random")


def tolerance_of(metric: str, baseline: float) -> float:
    """How far ``metric`` may slide from ``baseline`` before the build fails.

    A cost that was zero gets no allowance at all. "Up to a quarter of a model call per
    batch" is a sensible margin around one call and an unlimited licence around none, and
    the first model call a strategy makes is exactly the change worth seeing.
    """
    allowance = DEFAULT_TOLERANCES.get(metric, DEFAULT_TOLERANCE)
    if baseline == 0.0 and DIRECTIONS.get(metric) == "lower":
        return 0.0
    return allowance


#: Counts that may not shrink: they are the denominators every rate rests on. The
#: fraction is of the baseline's own value, so a fixture that grows does not need a
#: hand-written number here.
GATED_COUNTS: Final[tuple[str, ...]] = ("batches", "usable", "scored")
COUNT_TOLERANCE: Final = 0.02
#: Written into a baseline as well, and never gated: they are what says whether a
#: comparison against this floor is worth making at all (``comparable``).
RECORDED_COUNTS: Final[tuple[str, ...]] = BASIS_COUNTS

#: What a strategy **spends**, as opposed to what it achieves.
#:
#: Exempt from the floor comparison, and from that comparison only. A floor that calls
#: no model reports zero model calls and zero tokens, so holding any strategy that calls
#: one to "no worse than the floors" would be asking it not to exist — the gate would
#: refuse the engine ADR 0013 decided on, on the grounds that it does what the ADR says
#: to do. Cost is guarded where it can be: by the strategy's **own** committed baseline,
#: to a quarter of a model call and fifty tokens a card, and by the live plan that
#: prints what a run would spend before it spends it. The first number is still a number
#: somebody has to write into a file and defend in a diff.
FLOOR_EXEMPT: Final[tuple[str, ...]] = (
    "tmdb_calls_per_batch",
    "llm_calls_per_batch",
    "llm_tokens_per_card",
)


class BaselineError(ValueError):
    """The baseline file is not one, or is not about this run."""


@dataclass(frozen=True, slots=True)
class Regression:
    """One number that moved the wrong way, in the words the build will print."""

    metric: str
    baseline: float | None
    current: float | None
    tolerance: float

    def __str__(self) -> str:
        """Say what slipped, by how much, and what was allowed."""
        if self.baseline is None:
            return f"{self.metric}: not in the baseline (re-baseline to measure it)"
        if self.current is None:
            return f"{self.metric}: {_show(self.baseline)} -> n/a (the metric disappeared)"
        return (
            f"{self.metric}: {_show(self.baseline)} -> {_show(self.current)} "
            f"(tolerance {self.tolerance:g})"
        )


def _show(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


@dataclass(frozen=True, slots=True)
class Baseline:
    """The committed numbers a run is measured against."""

    dataset: str
    strategy: str
    options: Mapping[str, int | None]
    metrics: Mapping[str, float | None]
    counts: Mapping[str, int]
    tolerances: Mapping[str, float]

    @classmethod
    def of(cls, report: EvaluationReport) -> "Baseline":
        """Build a baseline from a report, with the default tolerances written in."""
        return cls(
            dataset=report.dataset,
            strategy=report.strategy,
            options=report.options.as_dict(),
            metrics=dict(report.metrics),
            counts={
                name: getattr(report.counts, name) for name in (*GATED_COUNTS, *RECORDED_COUNTS)
            },
            tolerances={
                key: tolerance_of(key, report.metrics.get(key) or 0.0)
                for key in sorted(DIRECTIONS)
                if DIRECTIONS[key] != "flat"
            },
        )

    def dump(self) -> str:
        """Return the canonical JSON text of the baseline, newline-terminated."""
        payload = {
            "version": 1,
            "dataset": self.dataset,
            "strategy": self.strategy,
            "options": dict(self.options),
            "counts": dict(self.counts),
            "metrics": dict(sorted(self.metrics.items())),
            "tolerances": dict(sorted(self.tolerances.items())),
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def write(self, path: Path) -> None:
        """Write the baseline to ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dump(), encoding="utf-8")

    @classmethod
    def loads(cls, text: str) -> "Baseline":
        """Parse a baseline, refusing a file that is not one."""
        try:
            payload = cast("object", json.loads(text))
        except json.JSONDecodeError as failure:
            raise BaselineError(f"the baseline is not JSON: {failure.msg}") from None
        if not isinstance(payload, dict):
            raise BaselineError("a baseline is a JSON object")
        entry = cast("Mapping[str, Any]", payload)
        if entry.get("version") != 1:
            raise BaselineError(f"unsupported baseline version {entry.get('version')!r}")
        return cls(
            dataset=_text(entry, "dataset"),
            strategy=_text(entry, "strategy"),
            options={
                key: _optional_int(key, value) for key, value in _mapping(entry, "options").items()
            },
            metrics={
                key: _optional_number(key, value)
                for key, value in _mapping(entry, "metrics").items()
            },
            counts={key: _int(key, value) for key, value in _mapping(entry, "counts").items()},
            tolerances={
                key: _number(key, value) for key, value in _mapping(entry, "tolerances").items()
            },
        )

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        """Read a baseline from ``path``."""
        try:
            return cls.loads(path.read_text(encoding="utf-8"))
        except OSError as failure:
            raise BaselineError(f"cannot read the baseline: {failure.strerror}") from None


def _text(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise BaselineError(f"a baseline's '{key}' is text")
    return value


def _mapping(entry: Mapping[str, Any], key: str) -> Mapping[str, object]:
    value = entry.get(key)
    if not isinstance(value, dict):
        raise BaselineError(f"a baseline's '{key}' is a JSON object")
    return cast("Mapping[str, object]", value)


def _int(key: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BaselineError(f"the baseline's '{key}' is a whole number")
    return value


def _optional_int(key: str, value: object) -> int | None:
    return None if value is None else _int(key, value)


def _number(key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BaselineError(f"the baseline's '{key}' is a number")
    return float(value)


def _optional_number(key: str, value: object) -> float | None:
    return None if value is None else _number(key, value)


def check_floor(floors: Sequence[Baseline], report: EvaluationReport) -> Sequence[Regression]:
    """Hold a strategy to the reference floors: it has to clear **one of them whole**.

    Without this the gate is only a per-strategy regression test, and the first baseline
    a new strategy writes is whatever it happened to score — so a strategy worse than
    "show the most popular thing you have not voted on" could pass for ever. The floors
    themselves are exempt: they are what everything else is measured against.

    **One floor, not the best of all of them.** The first version of this took the best
    value of every metric across the floors, and that bar is unbeatable by construction:
    the floors exist precisely because each is worse than the other somewhere, so the
    composite is a strategy that does not exist and that neither floor clears. What the
    gate means to ask — "is this better than doing something stupid?" — is answered by
    requiring the candidate to be no worse than **at least one** floor on every graded
    metric it supports. When none is cleared, the failure reported is against the floor
    it came closest to, so the output names one comparison rather than a mixture.

    Only the floors measured on the same vote set, with the same replay options, count.
    """
    if report.strategy in FLOOR_STRATEGIES:
        return []
    usable = _usable(floors, report)
    against = [(floor, _against(floor, report)) for floor in usable]
    if any(not found for _, found in against):
        return []
    return min(against, key=lambda entry: (len(entry[1]), entry[0].strategy))[1]


def _against(floor: Baseline, report: EvaluationReport) -> list[Regression]:
    """Every graded metric on which ``report`` is worse than this one floor."""
    found: list[Regression] = []
    for metric in sorted(DIRECTIONS):
        if DIRECTIONS[metric] == "flat" or metric in FLOOR_EXEMPT:
            continue
        # A rate measured on five scored cards is not comparable with the same rate
        # measured on sixty, on either side; the pool-free metrics are there precisely
        # so that something still is.
        if not comparable(metric, report.basis_counts) or not comparable(metric, floor.counts):
            continue
        was = floor.metrics.get(metric)
        if was is None:
            continue
        slid = _slid(metric, was, report.metrics.get(metric))
        if slid is not None:
            found.append(
                Regression(
                    f"floor:{floor.strategy}.{metric}", slid.baseline, slid.current, slid.tolerance
                )
            )
    return found


def uncomparable(floors: Sequence[Baseline], report: EvaluationReport) -> Sequence[str]:
    """Say which floor comparisons were **not** made, and why. Never silently skipped.

    A gate that quietly stops checking a metric is a gate nobody knows is off, so every
    comparison ``check_floor`` declined is named here and printed with the result.
    """
    if report.strategy in FLOOR_STRATEGIES:
        return []
    usable = _usable(floors, report)
    lines: list[str] = []
    for metric in sorted(DIRECTIONS):
        if DIRECTIONS[metric] == "flat":
            continue
        if metric in FLOOR_EXEMPT:
            lines.append(f"{metric}: not compared, a cost is its own baseline's business")
            continue
        if not comparable(metric, report.basis_counts):
            denominator = BASIS_COUNT[metric]
            lines.append(
                f"{metric}: not compared, this run put fewer than {MEANINGFUL_BASIS} cards "
                f"behind it ({denominator} {report.basis_counts[denominator]})"
            )
            continue
        supported = [
            floor
            for floor in usable
            if comparable(metric, floor.counts) and floor.metrics.get(metric) is not None
        ]
        if not supported:
            lines.append(f"{metric}: not compared, no floor's run supports it")
    return lines


def _usable(floors: Sequence[Baseline], report: EvaluationReport) -> Sequence[Baseline]:
    """Return the floors measured on this vote set with these options; refuse none."""
    found = [
        floor
        for floor in floors
        if floor.dataset == report.dataset and dict(floor.options) == report.options.as_dict()
    ]
    if not found:
        raise BaselineError(
            "no reference floor was measured on this vote set with these options; "
            f"run the {' and '.join(FLOOR_STRATEGIES)} baselines first"
        )
    return found


def check(baseline: Baseline, report: EvaluationReport) -> Sequence[Regression]:
    """Return every number that slid past its tolerance, ordered by metric name.

    Raises ``BaselineError`` when the two are not about the same run at all: a silent
    comparison of unlike things is the one failure a gate must never produce.
    """
    if baseline.dataset != report.dataset:
        raise BaselineError(
            f"the baseline is about '{baseline.dataset}', this run about '{report.dataset}'"
        )
    if baseline.strategy != report.strategy:
        raise BaselineError(
            f"the baseline is about '{baseline.strategy}', this run about '{report.strategy}'"
        )
    if dict(baseline.options) != report.options.as_dict():
        raise BaselineError("the baseline was walked with different replay options")
    return [*_metric_regressions(baseline, report), *_count_regressions(baseline, report)]


def _metric_regressions(baseline: Baseline, report: EvaluationReport) -> list[Regression]:
    """Compare every graded metric, walking the code's list rather than the file's.

    Walking the file's would mean a metric added later protects nothing until every
    baseline has been rewritten, and a metric deleted from a baseline protects nothing
    at all.
    """
    found: list[Regression] = []
    for metric in sorted(DIRECTIONS):
        if DIRECTIONS[metric] == "flat":
            continue
        if metric not in baseline.metrics:
            # Not "fine": unmeasured. Re-baselining is the deliberate act that fixes it.
            found.append(Regression(metric, None, report.metrics.get(metric), 0.0))
            continue
        slid = _slid(metric, baseline.metrics[metric], report.metrics.get(metric))
        if slid is not None:
            found.append(slid)
    return found


def _slid(metric: str, was: float | None, now: float | None) -> Regression | None:
    if was is None:
        # It had no denominator when the baseline was taken; there is nothing to slide
        # from, and a value appearing is an improvement.
        return None
    tolerance = tolerance_of(metric, was)
    if now is None:
        return Regression(metric, was, None, tolerance)
    direction = DIRECTIONS[metric]
    worse = (direction == "higher" and now < was - tolerance) or (
        direction == "lower" and now > was + tolerance
    )
    return Regression(metric, was, now, tolerance) if worse else None


def _count_regressions(baseline: Baseline, report: EvaluationReport) -> list[Regression]:
    found: list[Regression] = []
    for name in GATED_COUNTS:
        was = baseline.counts.get(name)
        if was is None:
            continue
        now: int = getattr(report.counts, name)
        floor = was * (1 - COUNT_TOLERANCE)
        if now < floor:
            found.append(Regression(f"counts.{name}", float(was), float(now), COUNT_TOLERANCE))
    return found
