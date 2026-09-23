"""The offline evaluation harness: replay recorded votes past a strategy, print numbers.

ADR 0013 puts this before the engine it is meant to judge. The pieces are deliberately
small and separately testable:

- ``dataset`` — the file: a catalogue of titles and one vote history per user.
- ``replay`` — the walk: reveal a prefix, ask for a batch, score it against what was
  held back.
- ``costs`` — what the batch spent, counted at the ports.
- ``metrics`` — the numbers, each with a direction and a denominator.
- ``gate`` — the committed baseline and the tolerance CI fails on.

``tindarr.main`` wires them to the real adapters (over a recorded cassette, or over the
network behind an explicit ``--live``); nothing in here knows what HTTP is.
"""

from tindarr.swipe.evaluation.costs import (
    BatchCost,
    CostMeter,
    CountingLlmProvider,
    CountingMetadata,
)
from tindarr.swipe.evaluation.dataset import (
    CatalogEntry,
    DatasetError,
    EvalDataset,
    EvalUser,
    FixtureVote,
    load_dataset,
)
from tindarr.swipe.evaluation.gate import (
    Baseline,
    BaselineError,
    Regression,
    check,
    check_floor,
    uncomparable,
)
from tindarr.swipe.evaluation.metrics import (
    BASIS,
    MEANINGFUL_BASIS,
    Counts,
    EvaluationReport,
    summarize,
)
from tindarr.swipe.evaluation.replay import (
    BatchOutcome,
    CardOutcome,
    ReplayError,
    ReplayOptions,
    replay,
)
from tindarr.swipe.strategy import StrategyFactory

__all__ = [
    "BASIS",
    "MEANINGFUL_BASIS",
    "Baseline",
    "BaselineError",
    "BatchCost",
    "BatchOutcome",
    "CardOutcome",
    "CatalogEntry",
    "CostMeter",
    "CountingLlmProvider",
    "CountingMetadata",
    "Counts",
    "DatasetError",
    "EvalDataset",
    "EvalUser",
    "EvaluationReport",
    "FixtureVote",
    "Regression",
    "ReplayError",
    "ReplayOptions",
    "check",
    "check_floor",
    "evaluate",
    "load_dataset",
    "replay",
    "summarize",
    "uncomparable",
]


async def evaluate(
    dataset: EvalDataset,
    factory: StrategyFactory,
    strategy_name: str,
    meter: CostMeter,
    options: ReplayOptions | None = None,
) -> EvaluationReport:
    """Replay ``dataset`` past the strategy ``factory`` builds, and return the report.

    The meter is passed in rather than made here: the caller is the one who wrapped the
    ports the strategy will reach, and a meter this function created would count nothing.
    Each batch is charged the difference between two readings of it, so what the report
    prints does not depend on the meter still holding the right number at the end.
    """
    settings = options or ReplayOptions()
    batches = await replay(dataset, factory, settings, meter)
    return summarize(dataset.name, dataset.source, strategy_name, batches, settings)
