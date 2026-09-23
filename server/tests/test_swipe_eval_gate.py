"""The CI gate and the cost meter: what fails a build, and what a batch is charged for."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from tests.support.evaluation import InMemoryMetadata, title
from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.llm import (
    Generation,
    LlmCapabilities,
    LlmProvider,
    LlmProviderKind,
    LlmUsage,
    Prompt,
)
from tindarr.ports.metadata import SearchQuery
from tindarr.ports.titles import TitleRef
from tindarr.swipe.evaluation import (
    Baseline,
    BaselineError,
    CostMeter,
    CountingLlmProvider,
    CountingMetadata,
    Counts,
    EvaluationReport,
    ReplayOptions,
    check,
    check_floor,
)
from tindarr.swipe.evaluation.gate import (
    COUNT_TOLERANCE,
    DEFAULT_TOLERANCE,
    Regression,
    tolerance_of,
)

pytestmark = pytest.mark.anyio


def report(**overrides: object) -> EvaluationReport:
    """A report with plausible numbers, for the gate to chew on."""
    metrics: dict[str, float | None] = {
        "fill_rate": 1.0,
        "usable_per_batch": 9.0,
        "complete_rate": 1.0,
        "waste_rate": 0.05,
        "coverage": 0.6,
        "already_seen_rate": 0.40,
        "like_rate": 0.35,
        "new_like_rate": 0.60,
        "agreement": 0.55,
        "skip_rate": 0.05,
        "genre_diversity": 0.7,
        "franchise_repeat_rate": 0.1,
        "tmdb_calls_per_batch": 10.0,
        "llm_calls_per_batch": 1.0,
        "llm_tokens_per_card": 300.0,
    }
    base = {
        "dataset": "synthetic-99",
        "source": "",
        "strategy": "popular",
        "options": ReplayOptions(),
        "counts": Counts(batches=10, usable=90, scored=60, complete=90),
        "metrics": metrics,
        "notes": (),
    }
    return EvaluationReport(**(base | overrides))  # pyright: ignore[reportArgumentType]


def moved(metric: str, value: float | None) -> EvaluationReport:
    """The same report with one metric moved."""
    current = report()
    return replace(current, metrics=dict(current.metrics) | {metric: value})


# --- the gate -------------------------------------------------------------------------


def test_a_baseline_round_trips_through_its_file(tmp_path: Path) -> None:
    baseline = Baseline.of(report())
    path = tmp_path / "baseline.json"
    baseline.write(path)
    reloaded = Baseline.load(path)

    assert reloaded.dump() == baseline.dump()
    assert check(reloaded, report()) == []
    assert reloaded.tolerances["like_rate"] == DEFAULT_TOLERANCE


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dataset", "other", "is about 'other'"),
        ("strategy", "hybrid", "is about 'hybrid'"),
    ],
)
def test_a_baseline_about_another_run_is_refused(field: str, value: str, message: str) -> None:
    baseline = Baseline.of(report(**{field: value}))
    with pytest.raises(BaselineError, match=message):
        check(baseline, report())


def test_a_baseline_walked_differently_is_refused() -> None:
    baseline = Baseline.of(report(options=ReplayOptions(batch_size=5)))
    with pytest.raises(BaselineError, match="different replay options"):
        check(baseline, report())


def test_a_rate_that_slides_past_its_tolerance_fails_the_build() -> None:
    baseline = Baseline.of(report())

    assert check(baseline, moved("like_rate", 0.34)) == []  # within two points
    slipped = check(baseline, moved("like_rate", 0.30))

    assert [row.metric for row in slipped] == ["like_rate"]
    assert "0.35 -> 0.3" in str(slipped[0])


def test_a_rate_that_should_go_down_fails_when_it_goes_up() -> None:
    baseline = Baseline.of(report())

    assert check(baseline, moved("already_seen_rate", 0.30)) == []  # better is fine
    assert [row.metric for row in check(baseline, moved("already_seen_rate", 0.50))] == [
        "already_seen_rate"
    ]


def test_a_metric_that_disappears_is_a_regression() -> None:
    baseline = Baseline.of(report())
    gone = check(baseline, moved("genre_diversity", None))

    assert [row.metric for row in gone] == ["genre_diversity"]
    assert "the metric disappeared" in str(gone[0])


def test_a_metric_missing_from_the_baseline_asks_to_be_re_baselined() -> None:
    # Walking the baseline's own list would mean a metric added later protects nothing
    # until every baseline has been rewritten — and a metric deleted from a baseline
    # protects nothing at all.
    baseline = Baseline.of(report())
    thin = Baseline(
        dataset=baseline.dataset,
        strategy=baseline.strategy,
        options=baseline.options,
        metrics={"like_rate": 0.35, "invented": 1.0},
        counts={},
        tolerances={},
    )
    missing = check(thin, report())

    assert "like_rate" not in [row.metric for row in missing]
    assert "already_seen_rate" in [row.metric for row in missing]
    assert "not in the baseline" in str(missing[0])


def test_a_metric_that_had_no_denominator_is_not_graded_until_it_does() -> None:
    baseline = Baseline.of(moved("genre_diversity", None))
    assert check(baseline, report()) == []


def test_the_tolerances_come_from_the_code_and_not_from_the_file() -> None:
    # A gate whose thresholds live inside the file it guards is switched off by a
    # one-token diff that looks like a number.
    baseline = Baseline.of(report())
    loosened = Baseline(
        dataset=baseline.dataset,
        strategy=baseline.strategy,
        options=baseline.options,
        metrics=baseline.metrics,
        counts=baseline.counts,
        tolerances={"like_rate": 0.9},
    )
    assert [row.metric for row in check(loosened, moved("like_rate", 0.0))] == ["like_rate"]


def test_a_cost_that_was_zero_gets_no_allowance() -> None:
    # "A quarter of a model call per batch" is a margin around one call and a licence
    # around none; the first model call a strategy makes is the change worth seeing.
    baseline = Baseline.of(report(metrics=dict(report().metrics) | {"llm_calls_per_batch": 0.0}))
    assert tolerance_of("llm_calls_per_batch", 0.0) == 0.0
    assert tolerance_of("llm_calls_per_batch", 1.0) == 0.25
    assert [row.metric for row in check(baseline, moved("llm_calls_per_batch", 0.1))] == [
        "llm_calls_per_batch"
    ]


def test_proposing_fewer_cards_cannot_buy_a_better_rate() -> None:
    baseline = Baseline.of(report())
    # A strategy that only proposes what it is sure of: every rate improves...
    flattering = replace(
        report(),
        counts=Counts(batches=10, usable=20, scored=20),
        metrics=dict(report().metrics) | {"like_rate": 0.95, "already_seen_rate": 0.05},
    )
    caught = check(baseline, flattering)

    # ...and the gate fails it on the denominators anyway.
    assert [row.metric for row in caught] == ["counts.usable", "counts.scored"]
    assert str(caught[0]).startswith("counts.usable: 90 -> 20")


def test_a_count_that_barely_moves_is_allowed() -> None:
    baseline = Baseline.of(report())
    steady = replace(report(), counts=Counts(batches=10, usable=89, scored=60))
    assert check(baseline, steady) == []
    assert COUNT_TOLERANCE == 0.02


def test_a_regression_reads_as_a_sentence() -> None:
    assert str(Regression("like_rate", None, 0.3, 0.02)) == (
        "like_rate: not in the baseline (re-baseline to measure it)"
    )
    assert str(Regression("like_rate", 0.35, None, 0.02)) == (
        "like_rate: 0.35 -> n/a (the metric disappeared)"
    )
    assert str(Regression("like_rate", 0.35, 0.1, 0.02)) == (
        "like_rate: 0.35 -> 0.1 (tolerance 0.02)"
    )


# --- the floor a new strategy has to clear ---------------------------------------------


def floors() -> list[Baseline]:
    """The two reference strategies, one better on likes, one worse on already-seen."""
    popular = Baseline.of(report(strategy="popular"))
    random_floor = Baseline.of(
        report(
            strategy="random",
            metrics=dict(report().metrics)
            | {"like_rate": 0.28, "already_seen_rate": 0.57, "new_like_rate": 0.66},
        )
    )
    return [popular, random_floor]


def test_a_new_strategy_is_held_to_the_best_of_the_floors() -> None:
    candidate = report(strategy="hybrid", metrics=dict(report().metrics) | {"like_rate": 0.30})
    slipped = check_floor(floors(), candidate)

    # The bar is per metric, and it is whichever floor did better on it: already-seen
    # 0.40 (popular's, the lower of the two), like rate 0.35 (popular's), new-like 0.66
    # (random's). The candidate matches the first and misses the other two.
    assert [row.metric for row in slipped] == ["floor.like_rate", "floor.new_like_rate"]
    assert "0.35 -> 0.3" in str(slipped[0])


def test_a_strategy_that_beats_both_floors_passes() -> None:
    better = report(
        strategy="hybrid",
        metrics=dict(report().metrics)
        | {"like_rate": 0.5, "already_seen_rate": 0.2, "new_like_rate": 0.7},
    )
    assert check_floor(floors(), better) == []


def test_the_floors_are_not_measured_against_each_other() -> None:
    # Each is worse than the other somewhere; holding them to one another would mean
    # neither could ever pass its own gate.
    assert check_floor(floors(), report(strategy="popular")) == []
    assert check_floor(floors(), report(strategy="random")) == []


def test_a_floor_measured_on_another_vote_set_does_not_count() -> None:
    elsewhere = [Baseline.of(report(strategy="popular", dataset="other"))]
    with pytest.raises(BaselineError, match="no reference floor"):
        check_floor(elsewhere, report(strategy="hybrid"))


def test_a_floor_without_a_metric_does_not_bar_anything() -> None:
    blank = [Baseline.of(report(strategy="popular", metrics={"like_rate": None}))]
    assert check_floor(blank, report(strategy="hybrid")) == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nope", "not JSON"),
        ("[]", "a baseline is a JSON object"),
        ('{"version": 2}', "unsupported baseline version"),
        ('{"version": 1}', "'dataset' is text"),
        ('{"version": 1, "dataset": "d", "strategy": "s"}', "'options' is a JSON object"),
        (
            '{"version": 1, "dataset": "d", "strategy": "s", "options": {"batch_size": "x"}}',
            "'batch_size' is a whole number",
        ),
        (
            '{"version": 1, "dataset": "d", "strategy": "s", "options": {},'
            ' "metrics": {"like_rate": "x"}}',
            "'like_rate' is a number",
        ),
        (
            '{"version": 1, "dataset": "d", "strategy": "s", "options": {},'
            ' "metrics": {}, "counts": {"usable": 1.5}}',
            "'usable' is a whole number",
        ),
        (
            '{"version": 1, "dataset": "d", "strategy": "s", "options": {},'
            ' "metrics": {}, "counts": {}, "tolerances": {"like_rate": true}}',
            "'like_rate' is a number",
        ),
    ],
)
def test_a_file_that_is_not_a_baseline_is_refused(text: str, message: str) -> None:
    with pytest.raises(BaselineError, match=message):
        Baseline.loads(text)


def test_a_baseline_that_cannot_be_read_says_so(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="cannot read the baseline"):
        Baseline.load(tmp_path / "missing.json")


# --- the cost meter -------------------------------------------------------------------


async def test_every_metadata_call_is_counted_and_passed_through() -> None:
    inner = InMemoryMetadata([title(1, "One")])
    meter = CostMeter()
    metadata = CountingMetadata(inner, meter)

    await metadata.test()
    await metadata.search(SearchQuery(title="One", kind="movie"))
    await metadata.match(SearchQuery(title="One", kind="movie"))
    await metadata.details(TitleRef("movie", 1), "en")
    await metadata.watch_providers(TitleRef("movie", 1), "FR")
    await metadata.trailer(TitleRef("movie", 1), "en")
    await metadata.region_providers("FR")

    assert meter.metadata_operations == (
        "test",
        "search",
        "match",
        "details",
        "watch_providers",
        "trailer",
        "region_providers",
    )
    assert meter.metadata_calls == 7


class _Answer(BaseModel):
    text: str


class FakeLlm:
    """An AI provider that answers instantly, with or without reporting its usage."""

    kind: LlmProviderKind = "openai"
    capabilities = LlmCapabilities()

    def __init__(self, usage: LlmUsage) -> None:
        self._usage = usage

    async def test(self) -> ConnectionCheck:
        return ConnectionCheck(health="ok")

    async def list_models(self) -> list[str]:
        return ["m"]

    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]:
        return Generation(value=schema.model_validate({"text": "ok"}), model="m", usage=self._usage)


async def test_tokens_a_provider_reports_are_measured() -> None:
    meter = CostMeter()
    provider: LlmProvider = CountingLlmProvider(FakeLlm(LlmUsage(120, 40)), meter)

    await provider.test()
    await provider.list_models()
    await provider.generate(Prompt(instructions="a", message="b"), _Answer)

    # test and list_models buy no cards, so they are not charged for.
    assert meter.llm_calls == 1
    assert (meter.llm_input_tokens, meter.llm_output_tokens) == (120, 40)
    assert meter.llm_calls_estimated == 0
    assert meter.tokens_fully_measured is True


async def test_tokens_a_provider_hides_are_estimated_and_declared() -> None:
    meter = CostMeter()
    provider = CountingLlmProvider(FakeLlm(LlmUsage()), meter)

    await provider.generate(Prompt(instructions="x" * 40, message="y" * 40), _Answer)

    assert meter.llm_calls_estimated == 1
    assert meter.llm_input_tokens == 20  # 80 characters, four to the token
    assert meter.llm_output_tokens > 0
    assert meter.tokens_fully_measured is False
    assert meter.llm_tokens == meter.llm_input_tokens + meter.llm_output_tokens
