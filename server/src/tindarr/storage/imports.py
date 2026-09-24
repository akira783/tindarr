"""The ``imports`` and ``import_reviews`` tables: one upload, and what it asked about.

**The uploaded file is never stored, and neither is its name.** What is kept is the
format that was detected, counts of what was read and dropped, and the rows the matcher
refused to guess at — which have to be kept, because the whole point of abstaining is
that somebody can come back and answer. Everything is keyed on the uploader: a review
entry is read and decided through a query scoped to their id, so another user's entry id
is a ``404`` and never a row somebody else can accept into their own history.

``candidates`` is the one JSON column here. It holds what TMDb offered for a row — ids,
titles, years, poster paths and a similarity — and it is parsed back through a pydantic
model rather than trusted, because a JSON column is a string an operator can edit and a
poster path that is not a path is a URL the console would load.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.ports.titles import MediaKind, TitleRef, as_media_kind
from tindarr.storage.ids import new_id
from tindarr.storage.tables import import_reviews, imports

__all__ = [
    "MAX_STORED_CANDIDATES",
    "ImportRecord",
    "ImportStatus",
    "NewReviewEntry",
    "ReviewCandidate",
    "ReviewEntryRecord",
    "ReviewStatus",
    "count_pending",
    "create",
    "decide",
    "finish",
    "get",
    "get_review",
    "list_for_user",
    "list_reviews",
    "list_running",
    "queue_reviews",
    "running_for_user",
]

type ImportStatus = Literal["running", "complete", "failed"]
type ReviewStatus = Literal["pending", "accepted", "rejected"]
#: How many candidates one review entry keeps. Enough to recognise the right title, few
#: enough that a long file cannot turn into a large database.
MAX_STORED_CANDIDATES: Final = 5
#: Longest stored row text. The parser already cuts a field; this is the backstop.
_MAX_QUERY_LENGTH: Final = 300


class ReviewCandidate(BaseModel):
    """One title TMDb offered for a row nobody has confirmed yet."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: Literal["movie", "tv"]
    tmdb_id: int = Field(gt=0)
    title: str = Field(max_length=_MAX_QUERY_LENGTH)
    year: int | None = None
    poster_path: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9._-]{1,128}$")
    similarity: float = Field(ge=0.0, le=1.0, default=0.0)

    @property
    def ref(self) -> TitleRef:
        """The title this candidate names."""
        return TitleRef(self.kind, self.tmdb_id)


@dataclass(frozen=True, slots=True)
class NewReviewEntry:
    """One question an import wants to ask, ready to be stored."""

    query: str
    kind_hint: MediaKind | None = None
    episodes: int = 0
    rating: float | None = None
    last_watched_at: datetime | None = None
    candidates: tuple[ReviewCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One row of ``imports``."""

    id: str
    user_id: str
    source: str
    status: str
    rows_read: int
    rows_skipped: str
    matched: int
    queued: int
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def skipped(self) -> dict[str, int]:
        """What the parser dropped, by reason, narrowed back out of its JSON column."""
        try:
            found: object = json.loads(self.rows_skipped)
        except ValueError:  # pragma: no cover - written by this module only
            return {}
        if not isinstance(found, dict):  # pragma: no cover - same
            return {}
        pairs = cast("dict[object, object]", found).items()
        return {str(name): value for name, value in pairs if isinstance(value, int)}


@dataclass(frozen=True, slots=True)
class ReviewEntryRecord:
    """One row of ``import_reviews``."""

    id: str
    import_id: str
    user_id: str
    query: str
    kind_hint: str | None
    episodes: int
    rating: float | None
    last_watched_at: datetime | None
    candidates: str
    status: str
    created_at: datetime
    decided_at: datetime | None

    @property
    def offered(self) -> tuple[ReviewCandidate, ...]:
        """What TMDb offered, narrowed; an entry whose JSON is broken offers nothing."""
        try:
            rows: object = json.loads(self.candidates)
        except ValueError:  # pragma: no cover - written by this module only
            return ()
        if not isinstance(rows, list):  # pragma: no cover - same
            return ()
        found: list[ReviewCandidate] = []
        for row in cast("list[object]", rows):
            try:
                found.append(ReviewCandidate.model_validate(row))
            except ValidationError:
                continue
        return tuple(found)

    @property
    def hint(self) -> MediaKind | None:
        """The media type the file guessed, when it guessed one."""
        return as_media_kind(self.kind_hint)


def _to_import(row: Row[tuple[Any, ...]]) -> ImportRecord:
    # Positional: ``ImportRecord`` lists the columns of ``imports`` in order.
    return ImportRecord(*row)


def _to_review(row: Row[tuple[Any, ...]]) -> ReviewEntryRecord:
    return ReviewEntryRecord(*row)


async def create(
    connection: AsyncConnection, user_id: str, source: str, *, now: datetime
) -> ImportRecord:
    """Open a ``running`` import for this user and return it."""
    record = ImportRecord(
        id=new_id(),
        user_id=user_id,
        source=source,
        status="running",
        rows_read=0,
        rows_skipped="{}",
        matched=0,
        queued=0,
        error_code=None,
        created_at=now,
        finished_at=None,
    )
    await connection.execute(
        imports.insert().values(
            id=record.id,
            user_id=record.user_id,
            source=record.source,
            status=record.status,
            rows_read=record.rows_read,
            rows_skipped=record.rows_skipped,
            matched=record.matched,
            queued=record.queued,
            error_code=None,
            created_at=record.created_at,
            finished_at=None,
        )
    )
    return record


async def finish(  # noqa: PLR0913 - one keyword per column an outcome fills in
    connection: AsyncConnection,
    import_id: str,
    *,
    status: ImportStatus,
    now: datetime,
    rows_read: int = 0,
    skipped: dict[str, int] | None = None,
    matched: int = 0,
    queued: int = 0,
    error_code: str | None = None,
) -> None:
    """Close an import with what it produced, or with the code that stopped it."""
    await connection.execute(
        update(imports)
        .where(imports.c.id == import_id)
        .values(
            status=status,
            rows_read=rows_read,
            rows_skipped=json.dumps(skipped or {}, sort_keys=True),
            matched=matched,
            queued=queued,
            error_code=error_code,
            finished_at=now,
        )
    )


async def get(connection: AsyncConnection, user_id: str, import_id: str) -> ImportRecord | None:
    """Return one of **this user's** imports, or ``None``."""
    statement = (
        imports.select().where(imports.c.id == import_id).where(imports.c.user_id == user_id)
    )
    row = (await connection.execute(statement)).one_or_none()
    return None if row is None else _to_import(row)


async def list_for_user(
    connection: AsyncConnection, user_id: str, limit: int = 20
) -> list[ImportRecord]:
    """Return this user's imports, most recent first."""
    statement = (
        imports.select()
        .where(imports.c.user_id == user_id)
        .order_by(imports.c.created_at.desc(), imports.c.id.desc())
        .limit(limit)
    )
    return [_to_import(row) for row in (await connection.execute(statement)).all()]


async def list_running(connection: AsyncConnection) -> list[ImportRecord]:
    """Return every unfinished import, whoever started it (the startup sweep)."""
    statement = imports.select().where(imports.c.status == "running")
    return [_to_import(row) for row in (await connection.execute(statement)).all()]


async def running_for_user(connection: AsyncConnection, user_id: str) -> ImportRecord | None:
    """Return this user's unfinished import, if they have one.

    One at a time, per user: an import costs TMDb requests and memory, and somebody
    uploading the same file ten times is either confused or trying something.
    """
    statement = (
        imports.select()
        .where(imports.c.user_id == user_id)
        .where(imports.c.status == "running")
        .limit(1)
    )
    row = (await connection.execute(statement)).one_or_none()
    return None if row is None else _to_import(row)


async def queue_reviews(
    connection: AsyncConnection,
    record: ImportRecord,
    entries: Sequence[NewReviewEntry],
    *,
    now: datetime,
) -> int:
    """Store the rows this import refused to guess at; return how many."""
    values = [
        {
            "id": new_id(),
            "import_id": record.id,
            "user_id": record.user_id,
            "query": entry.query[:_MAX_QUERY_LENGTH],
            "kind_hint": entry.kind_hint,
            "episodes": entry.episodes,
            "rating": entry.rating,
            "last_watched_at": entry.last_watched_at,
            "candidates": dump_candidates(entry.candidates),
            "status": "pending",
            "created_at": now,
            "decided_at": None,
        }
        for entry in entries
    ]
    if not values:
        return 0
    await connection.execute(import_reviews.insert().values(values))
    return len(values)


async def list_reviews(
    connection: AsyncConnection,
    user_id: str,
    import_id: str,
    *,
    status: ReviewStatus | None = "pending",
    limit: int = 100,
) -> list[ReviewEntryRecord]:
    """Return one import's review entries, oldest first, scoped to their owner."""
    statement = (
        import_reviews.select()
        .where(import_reviews.c.user_id == user_id)
        .where(import_reviews.c.import_id == import_id)
        .order_by(import_reviews.c.created_at, import_reviews.c.id)
        .limit(limit)
    )
    if status is not None:
        statement = statement.where(import_reviews.c.status == status)
    return [_to_review(row) for row in (await connection.execute(statement)).all()]


async def count_pending(connection: AsyncConnection, user_id: str, import_id: str) -> int:
    """How many questions this import still has open."""
    statement = (
        select(func.count())
        .select_from(import_reviews)
        .where(import_reviews.c.user_id == user_id)
        .where(import_reviews.c.import_id == import_id)
        .where(import_reviews.c.status == "pending")
    )
    return (await connection.execute(statement)).scalar_one()


async def get_review(
    connection: AsyncConnection, user_id: str, entry_id: str
) -> ReviewEntryRecord | None:
    """Return one of **this user's** review entries, or ``None``."""
    statement = (
        import_reviews.select()
        .where(import_reviews.c.id == entry_id)
        .where(import_reviews.c.user_id == user_id)
    )
    row = (await connection.execute(statement)).one_or_none()
    return None if row is None else _to_review(row)


async def decide(
    connection: AsyncConnection, user_id: str, entry_id: str, *, status: ReviewStatus, now: datetime
) -> bool:
    """Answer one pending entry, once. Says whether it was still open.

    A compare-and-set, like every other state change in this package: two tabs
    answering the same question must not both write a history row.
    """
    result = await connection.execute(
        update(import_reviews)
        .where(import_reviews.c.id == entry_id)
        .where(import_reviews.c.user_id == user_id)
        .where(import_reviews.c.status == "pending")
        .values(status=status, decided_at=now)
    )
    return result.rowcount == 1


def dump_candidates(candidates: Sequence[ReviewCandidate]) -> str:
    """Return the stored JSON for a row's candidates, capped and canonical."""
    return json.dumps(
        [candidate.model_dump(mode="json") for candidate in candidates[:MAX_STORED_CANDIDATES]],
        sort_keys=True,
    )
