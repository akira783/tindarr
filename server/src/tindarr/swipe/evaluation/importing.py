"""Building a private vote set from a real instance's database.

The committed fixture is generated, so it can only ever say whether the harness computes
what it claims to. Whether a strategy would please a real person is a question only real
votes answer, and those are not going in a public repository. This reads them out of a
database — Tindarr's own, or the SuggestArr fork's ``swipe_votes`` — into a file the
harness can replay and ``.gitignore`` keeps out of the history.

What comes across:

- the TMDb id, the media type and the vote, which is what a replay needs;
- the title, the year and the genres when the source has them, so the diversity metrics
  work;
- the **order** of the votes, as a sequence number. Not the dates.

What does not: the account id (users become ``user-1``, ``user-2``… in a fixed order),
the model's rationale for each card, poster paths, and every timestamp. None of it is
read by a metric, and all of it is the kind of thing that leaks when a file moves.

The database is opened read-only. A harness must never be the reason an instance's data
changed.
"""

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from tindarr.ports.titles import as_media_kind
from tindarr.swipe.evaluation.dataset import CatalogEntry, EvalDataset, EvalUser, FixtureVote
from tindarr.swipe.votes import as_vote_value

__all__ = ["ImportSummary", "VoteImportError", "import_votes"]

#: The fork's table, and the one roadmap step 4 gives Tindarr. Each is tried in turn.
_FORK_QUERY: Final = (
    "SELECT user_id, tmdb_id, media_type, vote, title, year, genres, pick_type "
    "FROM swipe_votes ORDER BY created_at, updated_at, tmdb_id"
)
_TINDARR_QUERY: Final = (
    "SELECT user_id, tmdb_id, media_kind, vote, NULL, NULL, NULL, pick "
    "FROM votes ORDER BY created_at, tmdb_id"
)
_SOURCES: Final[Mapping[str, tuple[str, str]]] = {
    "suggestarr": ("swipe_votes", _FORK_QUERY),
    "tindarr": ("votes", _TINDARR_QUERY),
}


class VoteImportError(ValueError):
    """The database cannot be read, or holds no vote table this knows."""


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """What the import found, and what it had to leave behind."""

    source: str
    users: int
    votes: int
    #: Rows whose vote value or TMDb id the source wrote in a shape nothing can use.
    dropped: int
    #: Titles the source knew enough about to build a catalogue entry from.
    catalogued: int

    def __str__(self) -> str:
        """One line for the terminal."""
        return (
            f"{self.votes} votes from {self.users} users out of a {self.source} database "
            f"({self.catalogued} titles catalogued, {self.dropped} rows dropped)"
        )


@dataclass(frozen=True, slots=True)
class _Row:
    user_id: str
    tmdb_id: int
    kind: str
    vote: str
    title: str | None
    year: int | None
    genres: tuple[str, ...]
    pick: str | None


def import_votes(
    database: Path, name: str, source: str | None = None
) -> tuple[EvalDataset, ImportSummary]:
    """Read a vote set out of ``database``, anonymised, and say what came across.

    ``source`` names the schema (``tindarr`` or ``suggestarr``); left out, both are
    tried and the first table that exists wins.
    """
    wanted = _schemas(source)
    # ``sqlite3.Connection`` as a context manager ends a transaction; it does not close
    # the file. ``closing`` is what puts the handle back.
    with closing(_open(database)) as connection:
        present = _tables(connection)
        for kind, (table, query) in wanted.items():
            if table not in present:
                continue
            rows, dropped = _read(connection, query)
            return _dataset(name, kind, rows, dropped)
    raise VoteImportError(
        "no vote table in this database: expected "
        + " or ".join(sorted(table for table, _ in wanted.values()))
    )


def _schemas(source: str | None) -> Mapping[str, tuple[str, str]]:
    if source is None:
        return _SOURCES
    if source not in _SOURCES:
        raise VoteImportError(f"unknown source '{source}'; expected {' or '.join(_SOURCES)}")
    return {source: _SOURCES[source]}


def _open(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise VoteImportError(f"no such database: {database}")
    try:
        # Read-only, by URI: the harness is never the reason an instance changed.
        return sqlite3.connect(f"file:{database.as_uri().removeprefix('file:')}?mode=ro", uri=True)
    except sqlite3.Error as failure:
        raise VoteImportError(f"cannot open the database: {failure}") from None


def _tables(connection: sqlite3.Connection) -> frozenset[str]:
    try:
        found = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return frozenset(str(row[0]) for row in found)
    except sqlite3.Error as failure:
        raise VoteImportError(f"cannot read the database: {failure}") from None


def _read(connection: sqlite3.Connection, query: str) -> tuple[list[_Row], int]:
    try:
        raw = connection.execute(query).fetchall()
    except sqlite3.Error as failure:
        raise VoteImportError(f"cannot read the votes: {failure}") from None
    rows: list[_Row] = []
    dropped = 0
    for entry in raw:
        row = _row_of(entry)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return rows, dropped


def _row_of(entry: Sequence[object]) -> _Row | None:
    kind = as_media_kind(entry[2])
    vote = as_vote_value(entry[3])
    tmdb_id = _whole(entry[1])
    if kind is None or vote is None or tmdb_id is None or tmdb_id <= 0:
        return None
    return _Row(
        user_id=str(entry[0]),
        tmdb_id=tmdb_id,
        kind=kind,
        vote=vote,
        title=_text(entry[4]),
        year=_whole(entry[5]),
        genres=_genres(entry[6]),
        pick=_text(entry[7]),
    )


def _whole(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _genres(value: object) -> tuple[str, ...]:
    """Read the fork's JSON list of genre names, which is usually absent."""
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    entries = cast("list[object]", parsed)
    names = {item.strip() for item in entries if isinstance(item, str) and item.strip()}
    return tuple(sorted(names))


def _dataset(
    name: str, kind: str, rows: Sequence[_Row], dropped: int
) -> tuple[EvalDataset, ImportSummary]:
    labels = {
        original: f"user-{position}"
        for position, original in enumerate(sorted({row.user_id for row in rows}), start=1)
    }
    users = [
        EvalUser(
            id=label,
            votes=tuple(
                FixtureVote(
                    seq=position,
                    tmdb_id=row.tmdb_id,
                    kind=row.kind,  # pyright: ignore[reportArgumentType] - narrowed above
                    vote=row.vote,  # pyright: ignore[reportArgumentType] - narrowed above
                    pick=row.pick if row.pick in ("safe", "explore", "calibration") else None,  # pyright: ignore[reportArgumentType]
                )
                for position, row in enumerate(
                    [entry for entry in rows if labels[entry.user_id] == label]
                )
            ),
        )
        for label in sorted(labels.values(), key=lambda value: int(value.removeprefix("user-")))
    ]
    catalog = _catalog(rows)
    dataset = EvalDataset(
        name=name,
        source=f"imported from a {kind} database; never committed",
        catalog=catalog,
        users=tuple(users),
    )
    summary = ImportSummary(
        source=kind,
        users=len(users),
        votes=sum(len(user.votes) for user in users),
        dropped=dropped,
        catalogued=len(catalog),
    )
    return dataset, summary


def _catalog(rows: Iterable[_Row]) -> tuple[CatalogEntry, ...]:
    entries: dict[tuple[str, int], CatalogEntry] = {}
    for row in rows:
        if row.title is None:
            continue
        entries.setdefault(
            (row.kind, row.tmdb_id),
            CatalogEntry(
                tmdb_id=row.tmdb_id,
                kind=row.kind,  # pyright: ignore[reportArgumentType] - narrowed above
                title=row.title,
                year=row.year,
                genres=row.genres,
            ),
        )
    return tuple(entries[key] for key in sorted(entries))
