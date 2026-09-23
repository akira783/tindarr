"""``tindarr eval``: the committed fixture, the gate, the importer, and the live guard.

The test that matters most is the dull one: the fixture in the repository is exactly
what the generator produces. A fixture nobody can regenerate is a fixture nobody can
review, and a harness whose inputs cannot be reviewed proves nothing.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from tindarr.main.cli import SYNTHETIC_SEED, main
from tindarr.main.evaluation import (
    DEFAULT_FIXTURES,
    STRATEGIES,
    EvalPaths,
    StrategySpec,
    build_cassette,
    live_plan,
)
from tindarr.swipe.baselines import PopularBaseline
from tindarr.swipe.evaluation import Baseline, ReplayOptions, load_dataset
from tindarr.swipe.evaluation.synthetic import build_synthetic_dataset

FIXTURES = Path(__file__).resolve().parent.parent / DEFAULT_FIXTURES


@pytest.fixture
def committed() -> EvalPaths:
    """The fixture directory the repository carries."""
    return EvalPaths(FIXTURES)


# --- the committed fixture is reproducible --------------------------------------------


def test_the_committed_vote_set_is_what_the_generator_produces(committed: EvalPaths) -> None:
    assert (
        committed.votes.read_text(encoding="utf-8")
        == build_synthetic_dataset(SYNTHETIC_SEED).dump()
    )


def test_the_committed_cassette_is_what_the_vote_set_needs(committed: EvalPaths) -> None:
    dataset = load_dataset(committed.votes)
    assert committed.cassette.read_text(encoding="utf-8") == build_cassette(dataset).dump()


def test_the_committed_titles_cannot_be_mistaken_for_real_ones(committed: EvalPaths) -> None:
    dataset = load_dataset(committed.votes)
    # A range TMDb does not use, so nothing here reads as scraped from anybody's API.
    assert all(entry.tmdb_id >= 900_000 for entry in dataset.catalog)
    assert dataset.name == "synthetic-99"


# --- running it ------------------------------------------------------------------------


def test_the_harness_runs_on_the_committed_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "run", "--fixtures", str(FIXTURES)]) == 0
    printed = capsys.readouterr().out
    assert "already_seen_rate" in printed
    assert "3 users, 9 batches" in printed


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_every_committed_baseline_still_holds(
    strategy: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["eval", "run", "--fixtures", str(FIXTURES), "--strategy", strategy, "--check"])
    assert code == 0
    assert "no regression" in capsys.readouterr().out


def test_a_worse_run_fails_the_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("votes.json", "tmdb.json"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())
    # A baseline claiming the strategy used to serve far fewer already-seen cards.
    baseline = Baseline.loads((FIXTURES / "baseline-popular.json").read_text(encoding="utf-8"))
    tampered = json.loads(baseline.dump())
    tampered["metrics"]["already_seen_rate"] = 0.2
    (tmp_path / "baseline-popular.json").write_text(json.dumps(tampered), encoding="utf-8")

    code = main(["eval", "run", "--fixtures", str(tmp_path), "--check"])

    printed = capsys.readouterr().out
    assert code == 1
    assert "already_seen_rate: 0.2 -> 0.85" in printed
    assert "--update-baseline" in printed


def test_a_baseline_can_be_rewritten_and_the_report_dumped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("votes.json", "tmdb.json"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())
    report = tmp_path / "out" / "report.json"

    code = main(
        [
            "eval",
            "run",
            "--fixtures",
            str(tmp_path),
            "--json",
            str(report),
            "--update-baseline",
            "--check",
        ]
    )

    assert code == 0
    assert "baseline written" in capsys.readouterr().out
    assert json.loads(report.read_text(encoding="utf-8"))["strategy"] == "popular"
    assert (tmp_path / "baseline-popular.json").is_file()


def test_the_replay_options_reach_the_run(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "eval",
            "run",
            "--fixtures",
            str(FIXTURES),
            "--batch-size",
            "5",
            "--warm-up",
            "20",
            "--max-batches",
            "1",
            "--seed",
            "9",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "batch_size=5, warm_up=20, max_batches=1, seed=9" in printed
    assert "3 users, 3 batches" in printed


# --- the things it refuses ------------------------------------------------------------


def test_an_unknown_strategy_is_refused() -> None:
    with pytest.raises(SystemExit):
        main(["eval", "run", "--strategy", "hybrid"])


def test_a_missing_vote_set_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "run", "--fixtures", str(tmp_path)]) == 2
    assert "cannot read the vote set" in capsys.readouterr().out


def test_a_missing_cassette_says_how_to_make_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "votes.json").write_bytes((FIXTURES / "votes.json").read_bytes())
    assert main(["eval", "run", "--fixtures", str(tmp_path)]) == 2
    assert "tindarr eval fixtures" in capsys.readouterr().out


def test_a_cassette_missing_one_answer_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "votes.json").write_bytes((FIXTURES / "votes.json").read_bytes())
    (tmp_path / "tmdb.json").write_text(
        json.dumps({"version": 1, "provider": "tmdb", "interactions": []}), encoding="utf-8"
    )
    assert main(["eval", "run", "--fixtures", str(tmp_path)]) == 2
    assert "no answer for" in capsys.readouterr().out


def test_a_broken_baseline_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("votes.json", "tmdb.json"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())
    (tmp_path / "baseline-popular.json").write_text("{", encoding="utf-8")
    assert main(["eval", "run", "--fixtures", str(tmp_path), "--check"]) == 2
    assert "not JSON" in capsys.readouterr().out


# --- a live run says what it will call ------------------------------------------------


def test_a_live_run_says_what_it_would_call_before_it_calls_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TINDARR_EVAL_TMDB_API_KEY", raising=False)
    code = main(["eval", "run", "--fixtures", str(FIXTURES), "--live", "--yes"])
    printed = capsys.readouterr().out

    assert code == 2  # no key, so nothing was called
    assert "A live run reaches real services" in printed
    assert "TMDb" in printed
    assert "nothing is billed" in printed


def test_a_strategy_that_would_call_a_model_says_how_many_times() -> None:
    spec = StrategySpec("hybrid", PopularBaseline, llm_calls_per_batch=2)
    dataset = build_synthetic_dataset(SYNTHETIC_SEED)
    plan = live_plan(spec, dataset, ReplayOptions())

    assert "AI provider up to 18 generations" in plan
    assert "billed by whichever provider" in plan


# --- regenerating the fixture ---------------------------------------------------------


def test_the_fixture_can_be_rebuilt_anywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "eval"
    assert main(["eval", "fixtures", "--fixtures", str(target)]) == 0

    assert "105 votes" in capsys.readouterr().out
    assert (target / "votes.json").read_text(encoding="utf-8") == (
        FIXTURES / "votes.json"
    ).read_text(encoding="utf-8")


# --- importing a private vote set -------------------------------------------------------


def fork_database(path: Path) -> Path:
    """A SuggestArr ``swipe_votes`` table with two users and one unusable row."""
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE swipe_votes (user_id INTEGER, tmdb_id TEXT, media_type TEXT, vote TEXT,"
        " title TEXT, year INTEGER, genres TEXT, rationale TEXT, pick_type TEXT,"
        " poster_path TEXT, requested INTEGER, created_at TEXT, updated_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO swipe_votes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                7,
                "603",
                "movie",
                "seen_liked",
                "The Matrix",
                1999,
                '["Action"]',
                "because you liked X",
                "safe",
                "/p.jpg",
                0,
                "2026-01-01",
                "2026-01-01",
            ),
            (
                7,
                "1396",
                "tv",
                "like",
                "Breaking Bad",
                2008,
                None,
                "a rationale",
                "explore",
                None,
                1,
                "2026-01-02",
                "2026-01-02",
            ),
            (
                9,
                "27205",
                "movie",
                "dislike",
                None,
                None,
                "not json",
                None,
                "nonsense",
                None,
                0,
                "2026-01-03",
                "2026-01-03",
            ),
            (9, "0", "movie", "like", "Broken", None, None, None, None, None, 0, "x", "x"),
            (9, "12", "movie", "not_a_vote", "Broken", None, None, None, None, None, 0, "x", "x"),
        ],
    )
    connection.commit()
    connection.close()
    return path


def test_a_fork_database_becomes_a_private_vote_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = fork_database(tmp_path / "suggestarr.db")
    out = tmp_path / "private" / "votes.json"

    code = main(["eval", "import", "--from", str(database), "--out", str(out), "--name", "mine"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "3 votes from 2 users" in printed
    assert "2 rows dropped" in printed
    dataset = load_dataset(out)
    # Account ids never travel; the rationale and the poster path do not either.
    assert [user.id for user in dataset.users] == ["user-1", "user-2"]
    assert "because you liked X" not in out.read_text(encoding="utf-8")
    assert dataset.users[0].votes[0].seq == 0
    assert {entry.title for entry in dataset.catalog} == {"The Matrix", "Breaking Bad"}
    assert dataset.catalog[0].genres in ((), ("Action",))


def test_a_tindarr_database_is_read_too(tmp_path: Path) -> None:
    database = tmp_path / "tindarr.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE votes (user_id TEXT, tmdb_id INTEGER, media_kind TEXT, vote TEXT,"
        " pick TEXT, created_at TEXT)"
    )
    connection.execute("INSERT INTO votes VALUES ('abc', 603, 'movie', 'like', 'safe', '1')")
    connection.commit()
    connection.close()
    out = tmp_path / "private" / "votes.json"

    assert main(["eval", "import", "--from", str(database), "--out", str(out)]) == 0
    dataset = load_dataset(out)
    assert dataset.users[0].votes[0].tmdb_id == 603
    assert dataset.users[0].id == "user-1"


def test_an_import_refuses_to_write_outside_a_private_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = fork_database(tmp_path / "suggestarr.db")
    code = main(["eval", "import", "--from", str(database), "--out", str(tmp_path / "votes.json")])

    printed = capsys.readouterr().out
    assert code == 2
    assert "somebody's viewing history" in printed
    assert not (tmp_path / "votes.json").exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--source", "tindarr"], "no vote table"),
        ([], "no vote table"),
    ],
)
def test_a_database_without_the_expected_table_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()
    out = tmp_path / "private" / "votes.json"

    code = main(["eval", "import", "--from", str(database), "--out", str(out), *arguments])

    assert code == 2
    assert message in capsys.readouterr().out


def test_a_database_that_is_not_there_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "private" / "votes.json"
    code = main(["eval", "import", "--from", str(tmp_path / "nope.db"), "--out", str(out)])
    assert code == 2
    assert "no such database" in capsys.readouterr().out
