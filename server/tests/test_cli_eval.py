"""``tindarr eval``: the committed fixture, the gate, the importer, and the live guard.

The test that matters most is the dull one: the fixture in the repository is exactly
what the generator produces. A fixture nobody can regenerate is a fixture nobody can
review, and a harness whose inputs cannot be reviewed proves nothing.
"""

import json
import sqlite3
from pathlib import Path

import httpx2
import pytest

from tindarr.adapters.cassette import Cassette, Interaction, TopUpTransport
from tindarr.main.cli import SYNTHETIC_SEED, main
from tindarr.main.evaluation import (
    AUTHOR_FIXTURES,
    RECORDED_LLM_BASE,
    STRATEGIES,
    SYNTHETIC_FIXTURES,
    EvalError,
    EvalPaths,
    Parts,
    StrategySpec,
    build_cassette,
    catalog_service,
    live_plan,
    popular_baseline,
    rehost,
)
from tindarr.swipe.evaluation import Baseline, ReplayOptions, load_dataset
from tindarr.swipe.evaluation.synthetic import build_synthetic_dataset
from tindarr.swipe.strategy import Candidate, StrategyContext

SERVER = Path(__file__).resolve().parent.parent
#: The generated vote set: reproducible from a seed, and the one these tests copy about.
FIXTURES = SERVER / SYNTHETIC_FIXTURES
#: The author's real 99 votes. Not reproducible from anything, so it is only ever read.
AUTHOR = SERVER / AUTHOR_FIXTURES


@pytest.fixture
def committed() -> EvalPaths:
    """The generated fixture directory the repository carries."""
    return EvalPaths(FIXTURES)


@pytest.fixture
def author() -> EvalPaths:
    """The author's own fixture directory."""
    return EvalPaths(AUTHOR)


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


@pytest.mark.parametrize("fixtures", [FIXTURES, AUTHOR], ids=["synthetic-99", "akira-99"])
@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_every_committed_baseline_still_holds(
    fixtures: Path, strategy: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI gate command itself, for every vote set and every strategy."""
    code = main(["eval", "run", "--fixtures", str(fixtures), "--strategy", strategy, "--check"])
    assert code == 0
    assert "no regression" in capsys.readouterr().out


@pytest.mark.parametrize("fixtures", [FIXTURES, AUTHOR], ids=["synthetic-99", "akira-99"])
def test_a_report_names_the_vote_set_its_numbers_came_from(
    fixtures: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two vote sets, one table layout: the name is the only thing telling them apart."""
    assert main(["eval", "run", "--fixtures", str(fixtures)]) == 0
    assert f"vote set   {fixtures.name}" in capsys.readouterr().out


# --- the author's own vote set ---------------------------------------------------------


def test_the_authors_vote_set_carries_no_trace_of_the_person(author: EvalPaths) -> None:
    """What the fixture format promises, checked on the one file where it matters."""
    dataset = load_dataset(author.votes)
    assert dataset.name == "akira-99"
    assert [user.id for user in dataset.users] == ["author"]
    for user in dataset.users:
        # A taste profile is prose an engine wrote about somebody; a library is what is
        # in their house. Neither is published, and neither is needed to score a replay.
        assert user.taste_profile is None
        assert user.library == ()
    assert not any(entry.overview for entry in dataset.catalog)
    # A vote is a rank, never a date: "watched this on that evening" is not published.
    assert sorted(row.seq for user in dataset.users for row in user.votes) == list(range(99))


def test_the_authors_cassette_covers_every_title_the_catalogue_holds(author: EvalPaths) -> None:
    """It was recorded from the real TMDb once; nothing can rebuild it, so it is checked.

    A generated cassette is held to its generator. This one is held to the only thing
    that still makes sense: it answers, with a 200, for every title an offline replay
    can be asked about.
    """
    dataset = load_dataset(author.votes)
    cassette = Cassette.load(author.cassette)
    for entry in dataset.catalog:
        # The adapter asks a series for its external ids as well, and sorts its query.
        query = (
            f"language={dataset.language}"
            if entry.kind == "movie"
            else f"append_to_response=external_ids&language={dataset.language}"
        )
        key = f"GET https://api.themoviedb.org/3/{entry.kind}/{entry.tmdb_id}?{query}"
        answer = cassette.find(key)
        assert answer is not None, key
        assert answer.status == 200
    # More than the catalogue since lot 4b: the discovery and recommendation pages the
    # pool is retrieved from, and the details of the titles they returned.
    assert len(cassette) > len(dataset.catalog)


def test_the_authors_cassette_carries_no_credential(author: EvalPaths) -> None:
    """It was recorded with a real key. None of the query parameters that carry one survive."""
    written = author.cassette.read_text(encoding="utf-8")
    assert "api_key" not in written
    assert "Authorization" not in written


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
    assert "already_seen_rate: 0.2 -> 0.857143" in printed
    assert "--update-baseline" in printed


def test_a_baseline_can_be_rewritten_and_the_report_dumped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("votes.json", "tmdb.json"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())
    report = tmp_path / "out" / "report.json"

    code = main(
        ["eval", "run", "--fixtures", str(tmp_path), "--json", str(report), "--update-baseline"]
    )

    assert code == 0
    assert "baseline written" in capsys.readouterr().out
    assert json.loads(report.read_text(encoding="utf-8"))["strategy"] == "popular"
    assert (tmp_path / "baseline-popular.json").is_file()


def test_a_baseline_cannot_be_rewritten_and_checked_in_one_breath() -> None:
    # Otherwise the run writes the numbers it is then compared to, and the gate can
    # never fail. argparse refuses the pair outright.
    with pytest.raises(SystemExit):
        main(["eval", "run", "--update-baseline", "--check"])


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
        main(["eval", "run", "--strategy", "clairvoyant"])


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


def test_a_strategy_that_would_call_a_model_says_how_many_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINDARR_EVAL_LLM_BASE_URL", "http://somewhere.example/v1")
    spec = StrategySpec("candidate", popular_baseline, needs_llm=True, llm_calls_per_batch=2)
    dataset = build_synthetic_dataset(SYNTHETIC_SEED)
    plan = live_plan(spec, dataset, ReplayOptions(), live=False, live_llm=True)

    assert "AI provider up to 18 generations" in plan
    # Where it goes, so nobody discovers afterwards which endpoint was billed.
    assert "http://somewhere.example/v1" in plan
    assert "TMDb        none" in plan


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


def test_a_regeneration_refuses_to_land_on_a_vote_set_no_seed_can_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``eval fixtures`` on the author's directory would replace real votes with invented
    ones, and the diff would look like any other regeneration."""
    target = tmp_path / "akira-99"
    target.mkdir()
    (target / "votes.json").write_bytes((AUTHOR / "votes.json").read_bytes())

    assert main(["eval", "fixtures", "--fixtures", str(target)]) == 2

    printed = capsys.readouterr().out
    assert "holds the 'akira-99' vote set" in printed
    assert (target / "votes.json").read_bytes() == (AUTHOR / "votes.json").read_bytes()


def test_a_regeneration_lands_on_a_file_that_is_not_a_vote_set_at_all(tmp_path: Path) -> None:
    """The guard protects vote sets, not rubble: an unreadable file is simply replaced."""
    target = tmp_path / "eval"
    target.mkdir()
    (target / "votes.json").write_text("not json", encoding="utf-8")

    assert main(["eval", "fixtures", "--fixtures", str(target)]) == 0
    assert load_dataset(target / "votes.json").name == "synthetic-99"


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


# --- the fixture service behind the cassette --------------------------------------------


def test_the_fixture_service_answers_404_for_a_title_it_does_not_have() -> None:
    dataset = build_synthetic_dataset(SYNTHETIC_SEED)
    cassette = build_cassette(dataset)
    # Details and recommendations for every title, plus the discovery pages: more than
    # the catalogue, and still exactly what the fixture can be asked. Anything else is a
    # miss, which is what stops a strategy inventing a title and the harness quietly
    # serving it one.
    assert len(cassette) > len(dataset.catalog)
    assert cassette.find("GET https://api.themoviedb.org/3/movie/1?language=en") is None


def test_a_live_run_records_what_came_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The "live" service here is the fixture's own, reached through the recording
    # transport: the point is that --record writes a cassette a later run can replay.
    dataset = build_synthetic_dataset(SYNTHETIC_SEED)
    monkeypatch.setattr(
        "tindarr.main.evaluation.httpx2.AsyncHTTPTransport",
        lambda: catalog_service(dataset),
    )
    monkeypatch.setenv("TINDARR_EVAL_TMDB_API_KEY", "a-real-looking-key")
    recorded = tmp_path / "tmdb.json"

    code = main(
        [
            "eval",
            "run",
            "--fixtures",
            str(FIXTURES),
            "--live",
            "--yes",
            "--record",
            str(tmp_path),
        ]
    )

    assert code == 0
    assert "A live run reaches real services" in capsys.readouterr().out
    replayed = Cassette.load(recorded)
    # Far fewer answers than calls: the three users share the famous cards, and the
    # discovery pages are read once per user and then cached for the rest of their run.
    assert 0 < len(replayed) < 160
    assert "a-real-looking-key" not in recorded.read_text(encoding="utf-8")


def test_recording_a_vote_set_writes_a_cassette_a_later_run_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``eval record`` is how a vote set of real titles gets its cassette.

    The "real TMDb" here is the fixture's own service, reached through the recording
    transport — the point being that the file it leaves behind covers the whole
    catalogue, so an offline run of the same directory needs nothing else.
    """
    dataset = build_synthetic_dataset(SYNTHETIC_SEED)
    monkeypatch.setattr(
        "tindarr.main.evaluation.httpx2.AsyncHTTPTransport",
        lambda: catalog_service(dataset),
    )
    monkeypatch.setenv("TINDARR_EVAL_TMDB_API_KEY", "a-real-looking-key")
    target = tmp_path / "eval"
    target.mkdir()
    (target / "votes.json").write_bytes((FIXTURES / "votes.json").read_bytes())

    assert main(["eval", "record", "--fixtures", str(target), "--yes"]) == 0

    printed = capsys.readouterr().out
    assert "A live recording reaches real services" in printed
    assert f"{len(dataset.catalog)} titles" in printed
    recorded = Cassette.load(target / "tmdb.json")
    # Details and recommendations per title, plus the discovery pages: the whole surface
    # the retrieval layer can ask this vote set for.
    assert len(recorded) > len(dataset.catalog)
    assert "a-real-looking-key" not in (target / "tmdb.json").read_text(encoding="utf-8")
    # And the directory now runs offline, which is the only thing the file is for.
    assert main(["eval", "run", "--fixtures", str(target)]) == 0


def test_a_recording_without_a_key_opens_no_connection_pool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same invariant as a live run: the key is checked before a transport is built."""
    opened: list[object] = []
    monkeypatch.setattr(
        "tindarr.main.evaluation.httpx2.AsyncHTTPTransport",
        lambda: opened.append(object()) or catalog_service(build_synthetic_dataset()),
    )
    monkeypatch.delenv("TINDARR_EVAL_TMDB_API_KEY", raising=False)

    assert main(["eval", "record", "--fixtures", str(FIXTURES), "--yes"]) == 2
    assert "a live run needs a TMDb key" in capsys.readouterr().out
    assert opened == []


def test_a_run_with_no_batches_at_all_still_prints() -> None:
    plan = live_plan(
        STRATEGIES["popular"],
        build_synthetic_dataset(SYNTHETIC_SEED),
        ReplayOptions(warm_up=500),
        live=True,
        live_llm=False,
    )
    assert "up to 0 requests" in plan


# --- a new strategy does not write its own floor ------------------------------------------


def test_a_new_strategy_is_held_to_the_committed_floors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strategy worse than both baselines fails --check even on its first run.

    Without this the gate is only a per-strategy regression test: the first baseline a
    new strategy writes is whatever it scored, so anything can certify itself.
    """
    for name in ("votes.json", "tmdb.json", "baseline-popular.json", "baseline-random.json"):
        (tmp_path / name).write_bytes((FIXTURES / name).read_bytes())

    class Sloppy:
        """Serves the household's own library, and never reads a title's details."""

        name = "candidate"

        def __init__(self, parts: Parts) -> None:
            self._parts = parts

        async def propose(self, context: StrategyContext, size: int) -> list[Candidate]:
            owned = sorted(context.library.refs)
            found = await self._parts.retrieval.pool(context)
            rest = [title.ref for title in found.titles if title.ref not in context.excluded]
            return [Candidate(ref=ref) for ref in (owned + rest)[:size]]

    monkeypatch.setitem(STRATEGIES, "candidate", StrategySpec("candidate", Sloppy))
    fixtures = ["--fixtures", str(tmp_path), "--strategy", "candidate"]
    main(["eval", "run", *fixtures, "--update-baseline"])
    capsys.readouterr()

    code = main(["eval", "run", *fixtures, "--check"])

    printed = capsys.readouterr().out
    assert code == 1
    # Its own baseline is happy — it wrote it. The floors are not.
    assert "floor:popular.complete_rate" in printed or "floor:random.complete_rate" in printed
    assert "worse than" in printed


def test_a_floor_strategy_is_not_measured_against_the_other_floor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `popular` is worse than `random` on already-seen and better on nothing much; each
    # is the yardstick, so neither is held to the other.
    for strategy in ("popular", "random"):
        assert (
            main(["eval", "run", "--fixtures", str(FIXTURES), "--strategy", strategy, "--check"])
            == 0
        )
    assert "no worse than the floors" in capsys.readouterr().out


def test_a_live_run_without_a_key_opens_no_connection_pool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Giving up must not leave a transport behind.

    One built before the key is checked is never closed, and its ``ResourceWarning``
    surfaces later, in whichever test the garbage collector happens to land on — which
    is how this arrived, as an intermittent failure three files away.
    """
    opened: list[object] = []
    monkeypatch.setattr(
        "tindarr.main.evaluation.httpx2.AsyncHTTPTransport",
        lambda: opened.append(object()) or catalog_service(build_synthetic_dataset()),
    )
    monkeypatch.delenv("TINDARR_EVAL_TMDB_API_KEY", raising=False)

    assert main(["eval", "run", "--fixtures", str(FIXTURES), "--live", "--yes"]) == 2
    assert "a live run needs a TMDb key" in capsys.readouterr().out
    assert opened == []


# --- the model cassette is portable, or it is not written -------------------------------


def test_a_recorded_model_answer_is_filed_under_a_neutral_address() -> None:
    """An OpenAI-compatible endpoint is somebody's own machine; its host is not ours."""
    recorded = Cassette(
        [Interaction(key="POST http://10.0.0.7/v1/chat/completions body:ab", status=200, body="{}")]
    )

    moved = rehost(recorded, "http://10.0.0.7:8000/v1", RECORDED_LLM_BASE)

    assert [row.key for row in moved.interactions] == [
        "POST http://recorded.invalid/v1/chat/completions body:ab"
    ]


def test_a_key_that_cannot_be_made_portable_stops_the_recording() -> None:
    """Silently keeping it would put the address in the repository, which is the point."""
    recorded = Cassette(
        [Interaction(key="POST http://elsewhere.lan/v1/x body:ab", status=200, body="{}")]
    )

    with pytest.raises(EvalError):
        rehost(recorded, "http://10.0.0.7:8000/v1", RECORDED_LLM_BASE)


@pytest.mark.anyio
async def test_a_live_run_answers_from_the_cassette_before_it_dials_out() -> None:
    """A live run extends a fixture. Replacing one breaks every strategy measured on it."""
    recorded = Cassette(
        [Interaction(key="GET https://api.themoviedb.org/3/movie/7", status=200, body='{"id": 7}')]
    )
    called: list[str] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        called.append(str(request.url))
        return httpx2.Response(500, json={})

    transport = TopUpTransport(recorded, httpx2.MockTransport(refuse))
    async with httpx2.AsyncClient(transport=transport) as client:
        stored = await client.get("https://api.themoviedb.org/3/movie/7")
        fresh = await client.get("https://api.themoviedb.org/3/movie/8")

    assert stored.json() == {"id": 7}
    assert called == ["https://api.themoviedb.org/3/movie/8"]
    assert fresh.status_code == 500
