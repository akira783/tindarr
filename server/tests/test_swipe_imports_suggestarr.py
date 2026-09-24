"""``tindarr import suggestarr``: the fork's votes, read once and never twice.

The properties worth pinning down here are the ones an operator cannot check by looking:
that the fork's database is not written to, that a second run is a no-op, that a vote
somebody has since changed in Tindarr is not quietly reverted to the fork's older
answer, and that an ambiguous user mapping refuses rather than guesses.
"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.ports.titles import TitleRef
from tindarr.storage import votes as vote_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.users import insert as insert_user
from tindarr.storage.users import new_user
from tindarr.swipe.imports import suggestarr

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

_SCHEMA = """
CREATE TABLE swipe_votes (
    user_id INTEGER NOT NULL,
    tmdb_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    vote TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    genres TEXT,
    rationale TEXT,
    pick_type TEXT,
    requested INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    poster_path TEXT,
    PRIMARY KEY (user_id, tmdb_id, media_type)
);
CREATE TABLE user_media_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    external_username TEXT NOT NULL,
    access_token TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verified INTEGER NOT NULL DEFAULT 0
);
"""

#: One row per thing worth reading differently: two verdicts, a series, a calibration
#: pick, a requested title, and three rows nothing can be made of.
_ROWS = [
    (1, "27205", "movie", "like", "Inception", 2010, "safe", 0, "2026-09-21 19:35:22"),
    (
        1,
        "1399",
        "tv",
        "seen_liked",
        "Game of Thrones",
        2011,
        "calibration",
        1,
        "2026-09-21 19:35:30",
    ),
    (1, "603", "movie", "dislike", "The Matrix", 1999, "explore", 0, "2026-09-21 19:35:40"),
    (1, "1396", "tv", "seen_disliked", "Breaking Bad", 2008, "safe", 0, "2026-09-21 19:35:50"),
]
_JUNK = [
    (1, "0", "movie", "like", "Zero", None, "safe", 0, "2026-09-21 19:36:00"),
    (1, "77", "kangaroo", "like", "Wrong kind", None, "safe", 0, "2026-09-21 19:36:10"),
    (1, "88", "movie", "shrugged", "Unknown verdict", None, "safe", 0, "2026-09-21 19:36:20"),
]


def _fork_database(path: Path, rows: list[object] | None = None, *, profiles: bool = False) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.executemany(
        "INSERT INTO swipe_votes "
        "(user_id, tmdb_id, media_type, vote, title, year, pick_type, requested, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows if rows is not None else _ROWS,
    )
    if profiles:
        connection.execute(
            "INSERT INTO user_media_profiles "
            "(user_id, provider, external_user_id, external_username, verified) "
            "VALUES (1, 'jellyfin', 'media-1', 'alex', 1)"
        )
    connection.commit()
    connection.close()
    return path


async def _user(engine: AsyncEngine, media_id: str = "media-1", name: str = "alex") -> str:
    async with write_transaction(engine) as connection:
        user = await insert_user(
            connection, new_user(media_id, name, NOW, admin=False, remote=True)
        )
    return user.id


def test_reads_every_vote_and_counts_what_it_cannot_read(tmp_path: Path) -> None:
    database = _fork_database(tmp_path / "requests.db", [*_ROWS, *_JUNK])

    votes, unreadable = suggestarr.read_votes(database)

    assert unreadable == len(_JUNK)
    assert [vote.ref for vote in votes] == [
        TitleRef("movie", 27205),
        TitleRef("tv", 1399),
        TitleRef("movie", 603),
        TitleRef("tv", 1396),
    ]
    assert [vote.value for vote in votes] == ["like", "seen_liked", "dislike", "seen_disliked"]
    assert [vote.pick for vote in votes] == ["safe", "calibration", "explore", "safe"]
    assert votes[1].requested is True
    assert votes[0].title == "Inception"
    assert votes[0].year == 2010
    assert votes[0].voted_at == datetime(2026, 9, 21, 19, 35, 22, tzinfo=UTC)


def test_a_row_without_a_title_falls_back_to_the_id_rather_than_inventing_one(
    tmp_path: Path,
) -> None:
    database = _fork_database(
        tmp_path / "requests.db",
        [(1, "27205", "movie", "like", None, None, "safe", 0, "2026-09-21 19:35:22")],
    )

    votes, _ = suggestarr.read_votes(database)

    assert votes[0].title == "TMDb 27205"


def test_refuses_a_file_that_is_not_a_suggestarr_database(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()

    with pytest.raises(suggestarr.ForkDatabaseError, match="swipe_votes"):
        suggestarr.read_votes(other)


def test_refuses_a_path_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(suggestarr.ForkDatabaseError, match="no database at"):
        suggestarr.read_votes(tmp_path / "missing.db")


def test_does_not_write_to_the_fork_database(tmp_path: Path) -> None:
    database = _fork_database(tmp_path / "requests.db", profiles=True)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    suggestarr.read_votes(database)
    suggestarr.read_identities(database)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not (tmp_path / "requests.db-wal").exists()


def test_a_receipt_is_stable_and_says_nothing_about_the_verdict(tmp_path: Path) -> None:
    database = _fork_database(tmp_path / "requests.db")
    votes, _ = suggestarr.read_votes(database)
    first = suggestarr.receipt_id(votes[0])

    # The same row, voted the other way in the fork after the first import.
    changed = _fork_database(
        tmp_path / "changed.db",
        [(1, "27205", "movie", "dislike", "Inception", 2010, "safe", 0, "2026-09-22 10:00:00")],
    )
    again, _ = suggestarr.read_votes(changed)

    assert first == suggestarr.receipt_id(again[0])
    assert first.startswith(f"{suggestarr.RECEIPT_NAMESPACE}:")


def test_reads_no_identities_when_the_fork_never_linked_one(tmp_path: Path) -> None:
    assert suggestarr.read_identities(_fork_database(tmp_path / "requests.db")) == ()


def test_reads_the_linked_media_server_account(tmp_path: Path) -> None:
    identities = suggestarr.read_identities(_fork_database(tmp_path / "requests.db", profiles=True))

    assert [identity.external_user_id for identity in identities] == ["media-1"]
    assert identities[0].provider == "jellyfin"


async def test_stores_every_vote_once(engine: AsyncEngine, tmp_path: Path) -> None:
    user_id = await _user(engine)
    votes, unreadable = suggestarr.read_votes(_fork_database(tmp_path / "requests.db"))

    async with write_transaction(engine) as connection:
        report = await suggestarr.store_votes(
            connection, user_id, votes, now=NOW, unreadable=unreadable
        )

    assert report.imported == 4
    assert report.already_imported == 0
    assert report.total == 4
    async with write_transaction(engine) as connection:
        assert await vote_repository.count(connection, user_id) == 4
        stored = await vote_repository.get(connection, user_id, TitleRef("tv", 1399))
    assert stored is not None
    assert stored.value == "seen_liked"
    assert stored.pick_type == "calibration"
    assert stored.title == "Game of Thrones"
    # The opinion did not come from a card this server served, and says so.
    assert stored.card_id is None


async def test_a_second_run_imports_nothing(engine: AsyncEngine, tmp_path: Path) -> None:
    user_id = await _user(engine)
    votes, _ = suggestarr.read_votes(_fork_database(tmp_path / "requests.db"))

    async with write_transaction(engine) as connection:
        await suggestarr.store_votes(connection, user_id, votes, now=NOW)
    async with write_transaction(engine) as connection:
        second = await suggestarr.store_votes(connection, user_id, votes, now=NOW)

    assert second.imported == 0
    assert second.already_imported == 4
    async with write_transaction(engine) as connection:
        assert await vote_repository.count(connection, user_id) == 4


async def test_does_not_revert_an_opinion_the_person_has_since_changed(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A newer Tindarr vote wins, and a re-import does not put the fork's answer back."""
    user_id = await _user(engine)
    votes, _ = suggestarr.read_votes(_fork_database(tmp_path / "requests.db"))
    async with write_transaction(engine) as connection:
        await suggestarr.store_votes(connection, user_id, votes, now=NOW)
    async with write_transaction(engine) as connection:
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=TitleRef("movie", 27205),
                value="dislike",
                card_id=None,
                pick_type="safe",
                title="Inception",
            ),
            voted_at=NOW + timedelta(days=1),
            now=NOW + timedelta(days=1),
        )

    async with write_transaction(engine) as connection:
        again = await suggestarr.store_votes(connection, user_id, votes, now=NOW)
        stored = await vote_repository.get(connection, user_id, TitleRef("movie", 27205))

    assert again.imported == 0
    assert stored is not None
    assert stored.value == "dislike"


async def test_an_older_fork_vote_loses_to_a_newer_tindarr_one(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Counted as superseded rather than imported, so the report does not overstate."""
    user_id = await _user(engine)
    votes, _ = suggestarr.read_votes(_fork_database(tmp_path / "requests.db"))
    async with write_transaction(engine) as connection:
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=TitleRef("movie", 27205),
                value="dislike",
                card_id=None,
                pick_type="safe",
                title="Inception",
            ),
            voted_at=NOW,
            now=NOW,
        )

    async with write_transaction(engine) as connection:
        report = await suggestarr.store_votes(connection, user_id, votes, now=NOW)
        stored = await vote_repository.get(connection, user_id, TitleRef("movie", 27205))

    assert report.superseded == 1
    assert report.imported == 3
    assert stored is not None
    assert stored.value == "dislike"


async def test_maps_the_fork_user_by_media_server_account(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    user_id = await _user(engine)
    identities = suggestarr.read_identities(_fork_database(tmp_path / "requests.db", profiles=True))

    async with write_transaction(engine) as connection:
        assert await suggestarr.resolve_target(connection, identities, 1, override=None) == user_id


async def test_refuses_when_the_fork_linked_no_media_server_account(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    await _user(engine)
    identities = suggestarr.read_identities(_fork_database(tmp_path / "requests.db"))

    async with write_transaction(engine) as connection:
        with pytest.raises(suggestarr.MappingError, match="--user"):
            await suggestarr.resolve_target(connection, identities, 1, override=None)


async def test_refuses_when_the_linked_account_is_unknown_here(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    await _user(engine, media_id="somebody-else")
    identities = suggestarr.read_identities(_fork_database(tmp_path / "requests.db", profiles=True))

    async with write_transaction(engine) as connection:
        with pytest.raises(suggestarr.MappingError, match="0 Tindarr accounts"):
            await suggestarr.resolve_target(connection, identities, 1, override=None)


async def test_the_user_override_is_checked_against_the_accounts_that_exist(
    engine: AsyncEngine,
) -> None:
    await _user(engine)

    async with write_transaction(engine) as connection:
        with pytest.raises(suggestarr.MappingError, match="no Tindarr user"):
            await suggestarr.resolve_target(connection, (), 1, override="nobody")


async def test_the_user_override_wins_over_the_mapping(engine: AsyncEngine, tmp_path: Path) -> None:
    other = await _user(engine, media_id="media-2", name="sam")
    await _user(engine)
    identities = suggestarr.read_identities(_fork_database(tmp_path / "requests.db", profiles=True))

    async with write_transaction(engine) as connection:
        assert await suggestarr.resolve_target(connection, identities, 1, override=other) == other


def test_reads_a_row_the_fork_wrote_with_other_column_types(tmp_path: Path) -> None:
    """``tmdb_id`` is a text column the fork sometimes fills with an integer."""
    database = tmp_path / "requests.db"
    connection = sqlite3.connect(database)
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO swipe_votes "
        "(user_id, tmdb_id, media_type, vote, title, year, pick_type, requested, created_at) "
        "VALUES (1, 27205, 'movie', 'like', ?, 2010, 'safe', 1, 'not a date')",
        ("x" * (suggestarr.MAX_FIELD_LENGTH + 50),),
    )
    connection.commit()
    connection.close()

    votes, unreadable = suggestarr.read_votes(database)

    assert unreadable == 0
    assert votes[0].ref == TitleRef("movie", 27205)
    assert len(votes[0].title) == suggestarr.MAX_FIELD_LENGTH
    # An unreadable timestamp is not a reason to drop an opinion; it just has no date.
    assert votes[0].voted_at is None
    assert votes[0].requested is True


def test_an_unknown_pick_type_is_read_as_a_safe_pick(tmp_path: Path) -> None:
    """The verdict is the load-bearing column; the pick kind is provenance."""
    database = _fork_database(
        tmp_path / "requests.db",
        [(1, "27205", "movie", "like", "Inception", 2010, "sideways", 0, "2026-09-21 19:35:22")],
    )

    votes, unreadable = suggestarr.read_votes(database)

    assert unreadable == 0
    assert votes[0].pick == "safe"


def test_refuses_a_file_that_is_not_a_database_at_all(tmp_path: Path) -> None:
    not_a_database = tmp_path / "notes.txt"
    not_a_database.write_text("this is not a database\n")

    with pytest.raises(suggestarr.ForkDatabaseError):
        suggestarr.read_votes(not_a_database)


def test_refuses_a_swipe_votes_table_it_cannot_read(tmp_path: Path) -> None:
    database = tmp_path / "requests.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE swipe_votes (something_else TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(suggestarr.ForkDatabaseError, match="cannot read"):
        suggestarr.read_votes(database)


def test_skips_a_linked_profile_that_names_nothing(tmp_path: Path) -> None:
    database = _fork_database(tmp_path / "requests.db")
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO user_media_profiles "
        "(user_id, provider, external_user_id, external_username, verified) "
        "VALUES (1, '', '', 'alex', 1)"
    )
    connection.commit()
    connection.close()

    assert suggestarr.read_identities(database) == ()
