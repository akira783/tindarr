"""The replay, the metrics and the gate: what the harness measures, and what it refuses.

The tests that matter most here are the ones about flattery. A harness that counts a
card nobody voted on as a hit, or that lets a strategy improve its rates by proposing
three cards instead of ten, would happily wave through the engine ADR 0013 rejected.
"""

from pathlib import Path

import pytest

from tests.support.evaluation import InMemoryMetadata, RecordedEngine, ScriptedStrategy
from tindarr.ports.titles import TitleRef
from tindarr.swipe.baselines import PopularBaseline
from tindarr.swipe.evaluation import (
    BatchCost,
    BatchOutcome,
    CostMeter,
    CountingMetadata,
    Counts,
    DatasetError,
    EvalDataset,
    EvaluationReport,
    ReplayError,
    ReplayOptions,
    evaluate,
    load_dataset,
    replay,
    summarize,
)
from tindarr.swipe.evaluation.metrics import DIRECTIONS
from tindarr.swipe.evaluation.synthetic import build_synthetic_dataset
from tindarr.swipe.strategy import Candidate, StrategyContext

pytestmark = pytest.mark.anyio

FULL = ReplayOptions(batch_size=10, warm_up=0)


def movie(tmdb_id: int) -> TitleRef:
    """A film reference, spelled once."""
    return TitleRef("movie", tmdb_id)


def small_dataset(votes: list[tuple[int, str]], **user: object) -> EvalDataset:
    """A one-user vote set with a catalogue covering everything it mentions."""
    ids = sorted({tmdb_id for tmdb_id, _ in votes} | {90, 91, 92, 93})
    return EvalDataset.model_validate(
        {
            "name": "small",
            "catalog": [
                {
                    "tmdb_id": tmdb_id,
                    "kind": "movie",
                    "title": f"T{tmdb_id}",
                    "genres": ["Drama"],
                    "popularity": float(tmdb_id),
                }
                for tmdb_id in ids
            ],
            "users": [
                {
                    "id": "user-1",
                    "votes": [
                        {"seq": seq, "tmdb_id": tmdb_id, "kind": "movie", "vote": value}
                        for seq, (tmdb_id, value) in enumerate(votes)
                    ],
                    **user,
                }
            ],
        }
    )


async def run(
    dataset: EvalDataset, strategy: ScriptedStrategy, options: ReplayOptions = FULL
) -> tuple[EvaluationReport, tuple[BatchOutcome, ...]]:
    """Replay one scripted strategy and return its report and its batches."""
    batches = await replay(dataset, lambda: strategy, options)
    report = summarize(dataset.name, "", strategy.name, batches, options)
    return report, batches


# --- the fixture reproduces what ADR 0013 measured ------------------------------------


def test_the_committed_fixture_carries_the_distribution_adr_0013_measured() -> None:
    dataset = build_synthetic_dataset()
    votes = [row.vote for user in dataset.users for row in user.votes]
    opinions = [value for value in votes if value != "skip"]
    seen = [value for value in opinions if value.startswith("seen_")]
    new = [value for value in opinions if not value.startswith("seen_")]

    assert len(opinions) == 99
    assert len(seen) / len(opinions) == pytest.approx(0.47, abs=0.01)
    assert new.count("like") / len(new) == pytest.approx(0.63, abs=0.01)


async def test_replaying_the_engine_that_produced_the_votes_reproduces_those_rates() -> None:
    dataset = build_synthetic_dataset()
    order = {
        user.id: [row.as_vote().ref for row in sorted(user.votes, key=lambda row: row.seq)]
        for user in dataset.users
    }
    engine = RecordedEngine(order)

    batches = await replay(dataset, lambda: engine, FULL)
    report = summarize(dataset.name, dataset.source, "recorded", batches, FULL)

    # Every card was one the user really voted on, so nothing was left unknown...
    assert report.counts.unknown == 0
    assert report.counts.scored == 99
    assert report.counts.skipped == 6
    assert report.counts.usable == 105
    assert report.metrics["coverage"] == pytest.approx(99 / 105)
    # ...and the two numbers ADR 0013 was written from come back out of the harness.
    assert report.metrics["already_seen_rate"] == pytest.approx(0.47, abs=0.01)
    assert report.metrics["new_like_rate"] == pytest.approx(0.63, abs=0.01)


# --- a card nobody voted on is never a win --------------------------------------------


async def test_a_card_nobody_voted_on_counts_as_nothing() -> None:
    dataset = small_dataset([(1, "like"), (2, "dislike")])
    strategy = ScriptedStrategy([[movie(1), movie(90), movie(91)]])

    report, _ = await run(dataset, strategy)

    assert report.counts.usable == 3
    assert report.counts.scored == 1
    assert report.counts.unknown == 2
    # One like out of one scored card, over three proposed: the rate is honest, and
    # coverage says how little it rests on.
    assert report.metrics["like_rate"] == 1.0
    assert report.metrics["coverage"] == pytest.approx(1 / 3)
    assert any("coverage" in note for note in report.notes)


async def test_a_strategy_proposing_only_unknown_titles_scores_nothing_at_all() -> None:
    dataset = small_dataset([(1, "like"), (2, "like")])
    report, _ = await run(dataset, ScriptedStrategy([[movie(90), movie(91)]]))

    assert report.metrics["like_rate"] is None
    assert report.metrics["coverage"] == 0.0
    assert any("rests on nothing" in note for note in report.notes)


async def test_a_skip_is_not_an_opinion() -> None:
    dataset = small_dataset([(1, "skip"), (2, "like")])
    report, _ = await run(dataset, ScriptedStrategy([[movie(1), movie(2)]]))

    assert report.counts.scored == 1
    assert report.counts.skipped == 1
    assert report.metrics["like_rate"] == 1.0
    assert report.metrics["skip_rate"] == 0.5


# --- the four ways a card is wasted ---------------------------------------------------


async def test_the_cards_a_strategy_was_told_to_avoid_are_waste_not_misses() -> None:
    dataset = small_dataset(
        [(index, "like" if index % 2 else "dislike") for index in range(1, 11)],
        library=[{"tmdb_id": 92, "kind": "movie"}],
    )
    # Batch 0 serves 90; batch 1 serves it again, repeats a title already voted on,
    # serves one the household owns, and duplicates a card within itself.
    strategy = ScriptedStrategy([[movie(90)], [movie(90), movie(1), movie(92), movie(9), movie(9)]])
    options = ReplayOptions(batch_size=5, warm_up=1)

    report, batches = await run(dataset, strategy, options)

    statuses = [card.status for card in batches[1].cards]
    assert statuses == ["served_again", "repeat", "owned", "like", "duplicate"]
    assert report.counts.served_again == 1
    assert report.counts.repeats == 1
    assert report.counts.owned == 1
    assert report.counts.duplicates == 1
    # Four of the six proposed cards were waste, and only the two usable ones are scored.
    assert report.metrics["waste_rate"] == pytest.approx(4 / 6)
    assert report.counts.usable == 2


# --- no strategy sees the future ------------------------------------------------------


async def test_a_strategy_only_ever_sees_the_votes_cast_before_its_batch() -> None:
    values = [(index, "like" if index % 2 else "dislike") for index in range(1, 21)]
    dataset = small_dataset(values)
    order = [movie(index) for index, _ in values]
    strategy = ScriptedStrategy([[] for _ in range(10)])

    await replay(dataset, lambda: strategy, ReplayOptions(batch_size=5, warm_up=3))

    assert [len(context.history) for context in strategy.seen] == [3, 8, 13, 18]
    for context in strategy.seen:
        revealed = len(context.history)
        assert [vote.ref for vote in context.history] == order[:revealed]
        # Nothing from the unrevealed tail is reachable from the context.
        assert context.voted.isdisjoint(order[revealed:])


async def test_a_user_whose_history_is_shorter_than_the_warm_up_is_not_replayed() -> None:
    dataset = small_dataset([(1, "like"), (2, "like")])
    batches = await replay(dataset, ScriptedStrategy, ReplayOptions(warm_up=5))
    assert batches == ()


async def test_a_run_stops_at_max_batches() -> None:
    dataset = small_dataset([(index, "like") for index in range(1, 21)])
    batches = await replay(
        dataset, ScriptedStrategy, ReplayOptions(batch_size=2, warm_up=0, max_batches=3)
    )
    assert len(batches) == 3


async def test_a_strategy_cannot_hand_back_more_cards_than_it_was_asked_for() -> None:
    dataset = small_dataset([(1, "like"), (2, "like"), (3, "like")])
    strategy = ScriptedStrategy([[movie(1), movie(2), movie(3)]])
    with pytest.raises(ReplayError, match="3 cards for a batch of 2"):
        await replay(dataset, lambda: strategy, ReplayOptions(batch_size=2, warm_up=0))


async def test_each_user_gets_a_strategy_of_their_own() -> None:
    dataset = build_synthetic_dataset()
    built: list[ScriptedStrategy] = []

    def factory() -> ScriptedStrategy:
        strategy = ScriptedStrategy([])
        built.append(strategy)
        return strategy

    await replay(dataset, factory, ReplayOptions(max_batches=1))
    assert len(built) == len(dataset.users)


# --- determinism ----------------------------------------------------------------------


async def test_two_runs_of_the_same_strategy_produce_the_same_document() -> None:
    dataset = build_synthetic_dataset()
    pool = dataset.pool

    async def once() -> str:
        meter = CostMeter()
        metadata = CountingMetadata(InMemoryMetadata(pool), meter)
        report = await evaluate(dataset, lambda: PopularBaseline(pool, metadata), "popular", meter)
        return report.to_json()

    assert await once() == await once()


# --- diversity is read from the fixture, not from the strategy ------------------------


async def test_diversity_counts_genres_and_franchise_repeats_from_the_catalogue() -> None:
    dataset = EvalDataset.model_validate(
        {
            "name": "diverse",
            "catalog": [
                {
                    "tmdb_id": 1,
                    "kind": "movie",
                    "title": "A",
                    "genres": ["Drama"],
                    "franchise": "Saga",
                },
                {
                    "tmdb_id": 2,
                    "kind": "movie",
                    "title": "B",
                    "genres": ["Drama"],
                    "franchise": "Saga",
                },
                {"tmdb_id": 3, "kind": "movie", "title": "C", "genres": ["Comedy"]},
            ],
            "users": [
                {
                    "id": "user-1",
                    "votes": [
                        {"seq": 0, "tmdb_id": 1, "kind": "movie", "vote": "like"},
                        {"seq": 1, "tmdb_id": 2, "kind": "movie", "vote": "like"},
                        {"seq": 2, "tmdb_id": 3, "kind": "movie", "vote": "like"},
                    ],
                }
            ],
        }
    )
    strategy = ScriptedStrategy([[movie(1), movie(2), movie(3)]])
    report, batches = await run(dataset, strategy, ReplayOptions(batch_size=3, warm_up=0))

    assert batches[0].known == 3
    assert batches[0].distinct_genres == 2
    assert batches[0].franchise_repeat is True
    assert report.metrics["genre_diversity"] == pytest.approx(2 / 3)
    assert report.metrics["franchise_repeat_rate"] == 1.0


async def test_a_vote_set_without_a_catalogue_says_so_instead_of_guessing() -> None:
    dataset = EvalDataset.model_validate(
        {
            "name": "bare",
            "users": [
                {
                    "id": "user-1",
                    "votes": [
                        {"seq": 0, "tmdb_id": 1, "kind": "movie", "vote": "like"},
                        {"seq": 1, "tmdb_id": 2, "kind": "movie", "vote": "dislike"},
                    ],
                }
            ],
        }
    )
    report, _ = await run(dataset, ScriptedStrategy([[movie(1), movie(2)]]))

    assert report.metrics["genre_diversity"] is None
    assert report.metrics["franchise_repeat_rate"] is None
    assert any("no catalogue" in note for note in report.notes)


# --- the table and the JSON -----------------------------------------------------------


async def test_the_table_names_every_metric_and_the_cost_it_charged() -> None:
    dataset = build_synthetic_dataset()
    pool = dataset.pool
    meter = CostMeter()
    metadata = CountingMetadata(InMemoryMetadata(pool), meter)
    report = await evaluate(dataset, lambda: PopularBaseline(pool, metadata), "popular", meter)

    table = report.table()

    assert "already_seen_rate" in table
    assert "note  " in table
    assert report.counts.tmdb_calls == meter.metadata_calls > 0
    assert report.as_dict()["strategy"] == "popular"
    # Nothing was guessed in this run, so nothing claims to have been.
    assert "estimated" not in table
    assert report.kinds["llm_tokens_per_card"] == "measured"


def bare_report(counts: Counts) -> EvaluationReport:
    """A report with no metrics, for the parts that only read the counts."""
    return EvaluationReport(
        dataset="d",
        source="",
        strategy="s",
        options=ReplayOptions(),
        counts=counts,
        metrics=dict.fromkeys(DIRECTIONS),
        notes=(),
    )


def test_tokens_are_called_estimated_only_when_a_provider_withheld_them() -> None:
    measured = bare_report(Counts(llm_calls=2))
    guessed = bare_report(Counts(llm_calls=2, llm_calls_estimated=1))

    assert measured.kinds["llm_tokens_per_card"] == "measured"
    assert guessed.kinds["llm_tokens_per_card"] == "estimated"
    assert "estimated" in guessed.table()


def test_a_run_says_how_many_calls_it_had_to_guess_the_tokens_of() -> None:
    batch = BatchOutcome(
        user_id="user-1",
        index=0,
        requested=1,
        cards=(),
        cost=BatchCost(llm_calls=2, llm_input_tokens=40, llm_calls_estimated=1),
        known=0,
        distinct_genres=None,
        franchise_repeat=None,
    )
    report = summarize("d", "", "s", [batch], ReplayOptions())

    assert report.counts.llm_calls == 2
    assert report.counts.llm_calls_estimated == 1
    assert any("estimated at four characters" in note for note in report.notes)


# --- the file format ------------------------------------------------------------------


def test_a_vote_set_carries_what_the_replay_needs() -> None:
    dataset = EvalDataset.model_validate(
        {
            "name": "shapes",
            "language": "fr",
            "region": "FR",
            "catalog": [
                {
                    "tmdb_id": 7,
                    "kind": "tv",
                    "title": "Sept",
                    "year": 2001,
                    "genres": ["Drama"],
                    "popularity": 3.5,
                    "vote_average": 8.1,
                    "vote_count": 12,
                    "overview": "Seven.",
                }
            ],
            "users": [
                {
                    "id": "user-1",
                    "media_kind": "tv",
                    "novelty": "bold",
                    "mood": "something slow",
                    "library": [{"tmdb_id": 42, "kind": "movie"}],
                    "votes": [
                        {"seq": 3, "tmdb_id": 7, "kind": "tv", "vote": "like", "pick": "explore"},
                        {"seq": 1, "tmdb_id": 9, "kind": "movie", "vote": "skip"},
                    ],
                }
            ],
        }
    )
    user = dataset.users[0]

    # Votes come back in their own order, whatever order the file listed them in.
    assert [row.ref.tmdb_id for row in user.ordered_votes] == [9, 7]
    assert user.ordered_votes[0].at < user.ordered_votes[1].at
    assert user.wanted_kind == "tv"
    assert user.novelty_level == "bold"
    assert user.library_index.owns(TitleRef("movie", 42))
    assert dataset.pool[0].title == "Sept"
    assert dataset.pool[0].ref == TitleRef("tv", 7)
    assert dataset.by_ref[TitleRef("tv", 7)].year == 2001
    assert EvalDataset.loads(dataset.dump()) == dataset


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "JSON"),
        ('{"name": "x", "surprise": 1}', "surprise"),
        ('{"name": "x", "catalog": [{"tmdb_id": 0, "kind": "movie", "title": "t"}]}', "tmdb_id"),
        ('{"name": "x", "catalog": [{"tmdb_id": 1, "kind": "film", "title": "t"}]}', "kind"),
        (
            '{"name": "x", "users": [{"id": "u", "votes":'
            ' [{"seq": 0, "tmdb_id": 1, "kind": "movie", "vote": "loved"}]}]}',
            "vote",
        ),
        ('{"version": 2, "name": "x"}', "version"),
    ],
)
def test_a_file_that_is_not_a_vote_set_is_refused(text: str, message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        EvalDataset.loads(text)


def test_a_vote_set_that_cannot_be_read_says_so(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="cannot read the vote set"):
        load_dataset(tmp_path / "missing.json")


def test_a_vote_set_round_trips_through_its_file(tmp_path: Path) -> None:
    dataset = build_synthetic_dataset()
    path = tmp_path / "nested" / "votes.json"
    dataset.write(path)
    assert load_dataset(path) == dataset


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"batch_size": 0}, "at least one card"),
        ({"warm_up": -1}, "cannot be negative"),
        ({"max_batches": 0}, "at least one batch"),
    ],
)
def test_replay_options_that_would_make_a_report_meaningless_are_refused(
    options: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ReplayOptions(**options)


# --- what a strategy cannot do to the numbers -------------------------------------------


async def test_a_strategy_cannot_empty_the_library_to_hide_what_it_owns() -> None:
    # The index in the context is rebuilt per batch and scoring never consults it: an
    # object a strategy holds is an object a strategy can clear.
    dataset = small_dataset(
        [(index, "like") for index in range(1, 11)], library=[{"tmdb_id": 92, "kind": "movie"}]
    )

    class Wiper:
        name = "wiper"

        async def propose(self, context: StrategyContext, size: int) -> list[Candidate]:
            context.library._by_ref = {}  # pyright: ignore[reportPrivateUsage]
            return [Candidate(ref=movie(92))]

    batches = await replay(dataset, Wiper, ReplayOptions(batch_size=1, warm_up=1, max_batches=1))

    assert [card.status for batch in batches for card in batch.cards] == ["owned"]


async def test_the_cost_meter_cannot_be_reset() -> None:
    meter = CostMeter()
    meter.record_metadata("details")
    with pytest.raises(AttributeError):
        meter.metadata_calls = 0  # pyright: ignore[reportAttributeAccessIssue]
    assert meter.metadata_calls == 1


async def test_a_cost_that_goes_backwards_stops_the_run() -> None:
    dataset = small_dataset([(index, "like") for index in range(1, 11)])
    meter = CostMeter()
    meter.record_metadata("details")

    class Eraser:
        name = "eraser"

        async def propose(self, context: StrategyContext, size: int) -> list[Candidate]:
            meter._metadata.clear()  # pyright: ignore[reportPrivateUsage]
            return []

    with pytest.raises(ReplayError, match="went backwards"):
        await replay(dataset, Eraser, ReplayOptions(batch_size=1, warm_up=1), meter)


async def test_a_card_without_its_details_is_not_a_free_card() -> None:
    # Otherwise "made no TMDb call" is a perfect score on the cost axis for cards that
    # cannot be rendered.
    dataset = small_dataset([(1, "like"), (2, "like"), (3, "like")])
    report, _ = await run(dataset, ScriptedStrategy([[movie(1), movie(2)]]))

    assert report.metrics["complete_rate"] == 0.0
    assert report.metrics["tmdb_calls_per_batch"] == 0.0


async def test_a_smaller_batch_cannot_out_diversify_a_bigger_one() -> None:
    dataset = EvalDataset.model_validate(
        {
            "name": "sizes",
            "catalog": [
                {
                    "tmdb_id": index,
                    "kind": "movie",
                    "title": f"T{index}",
                    "genres": [f"G{index}"],
                }
                for index in range(1, 11)
            ],
            "users": [
                {
                    "id": "user-1",
                    "votes": [
                        {"seq": index, "tmdb_id": index, "kind": "movie", "vote": "like"}
                        for index in range(1, 11)
                    ],
                }
            ],
        }
    )
    options = ReplayOptions(batch_size=10, warm_up=0, max_batches=1)
    three, _ = await run(dataset, ScriptedStrategy([[movie(1), movie(2), movie(3)]]), options)
    ten, _ = await run(
        dataset, ScriptedStrategy([[movie(index) for index in range(1, 11)]]), options
    )

    # Three cards, three genres: one genre per card, not more than the full batch's.
    assert three.metrics["genre_diversity"] == pytest.approx(0.3)
    assert ten.metrics["genre_diversity"] == pytest.approx(1.0)


async def test_unknown_cards_cannot_drive_the_skip_rate_down() -> None:
    # The denominator is the cards the fixture has an opinion on, not every card that
    # was not waste: padding a batch with titles nobody voted on must not look like a
    # strategy nobody skips.
    dataset = small_dataset([(1, "skip"), (2, "like")])
    padded, _ = await run(dataset, ScriptedStrategy([[movie(1), movie(2), movie(90), movie(91)]]))

    assert padded.counts.unknown == 2
    assert padded.metrics["skip_rate"] == 0.5
