"""The CI gate: a committed baseline, a tolerance, and a build that fails on a slide.

ADR 0013 says no strategy ships without numbers. A number nobody compares is a number
nobody reads, so the harness's numbers on the committed vote set are themselves
committed, and a run that comes out worse than them by more than a stated margin fails
the build.

Three things make the gate hard to walk past:

- **The run has to be the same run.** A baseline records the vote set, the strategy and
  the replay options. Comparing a report walked with a different batch size, or produced
  by another strategy, is refused rather than quietly allowed.
- **The counts are gated too, and tightly.** The cheapest way to improve every rate is
  to propose fewer cards: three confident picks instead of ten score beautifully. So
  ``batches``, ``usable`` and ``scored`` may not fall at all beyond a small margin,
  whatever the rates do.
- **A metric that disappears is a regression.** A rate that becomes ``n/a`` because its
  denominator emptied is not an improvement.

Updating a baseline is meant to be a deliberate act with a diff: ``tindarr eval baseline
--write`` regenerates it, the diff shows every number that moved, and the reason belongs
in the commit message.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from tindarr.swipe.evaluation.metrics import DIRECTIONS, EvaluationReport

__all__ = [
    "GATED_COUNTS",
    "Baseline",
    "BaselineError",
    "Regression",
    "check",
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
#: Counts that may not shrink: they are the denominators every rate rests on. The
#: fraction is of the baseline's own value, so a fixture that grows does not need a
#: hand-written number here.
GATED_COUNTS: Final[tuple[str, ...]] = ("batches", "usable", "scored")
COUNT_TOLERANCE: Final = 0.02


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
            counts={name: getattr(report.counts, name) for name in GATED_COUNTS},
            tolerances={
                key: DEFAULT_TOLERANCES.get(key, DEFAULT_TOLERANCE)
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


def check(baseline: Baseline, report: EvaluationReport) -> Sequence[Regression]:
    """Return every number that slid past its tolerance, worst first by metric name.

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
    found: list[Regression] = []
    for metric in sorted(baseline.metrics):
        direction = DIRECTIONS.get(metric)
        if direction is None or direction == "flat":
            continue
        was = baseline.metrics[metric]
        now = report.metrics.get(metric)
        if was is None:
            continue
        tolerance = baseline.tolerances.get(metric, DEFAULT_TOLERANCE)
        if now is None:
            found.append(Regression(metric, was, None, tolerance))
        elif (direction == "higher" and now < was - tolerance) or (
            direction == "lower" and now > was + tolerance
        ):
            found.append(Regression(metric, was, now, tolerance))
    return found


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
