"""The two ways a household says what it has already seen, and the one place they land.

``tindarr.swipe.imports`` and ``tindarr.swipe.calibration`` are deliberately pure: they
take bytes, or a TMDb port, and give back values. This is the layer that has a database
and a clock — it reads the household's settings, runs one of those two, writes the rows
and answers the questions an import left open.

Three properties are worth stating here rather than leaving to a reader to reconstruct.

**An import can only ever be wrong about the person who uploaded it.** Every write is
keyed on their user id, every read is scoped to it, and there is no shared catalogue to
corrupt: a history row is a TMDb id and a verdict about one household's evening. Two
users importing contradictory files about the same film produce two rows and no conflict.

**Nothing an import writes is a vote.** The stats, the taste profile's vote history and
every strategy replay read votes; these rows are read by ``StrategyContext.known`` and
``engagement`` and by nothing else. A person who imports ten thousand films has a
calibrated deck and a vote count of zero.

**An import holds no file.** The bytes are parsed in the request that carried them and
are never written down; what survives is the titles that were identified and the rows
that were not. The uploaded file name is not stored either — it is a string the user's
own computer chose, it says nothing the detected format does not, and it would otherwise
reach a console, a log line and a backup.
"""

import logging
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.connectors import ConnectorService
from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.history import IMPORT_SOURCES, HistorySource, WatchedTitle, as_history_source
from tindarr.ports.metadata import Metadata, TitleFilters
from tindarr.ports.titles import TitleRef
from tindarr.storage import history as history_repository
from tindarr.storage import imports as import_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.imports import ImportRecord, NewReviewEntry, ReviewCandidate
from tindarr.storage.settings import ContentFilters, SettingsStore
from tindarr.swipe.calibration import (
    CalibrationGrid,
    GridRequest,
    GridTick,
    GridTitle,
    grid_history,
)
from tindarr.swipe.imports import (
    ImportOutcome,
    MetadataDownError,
    ParsedFile,
    UnreadableImportError,
    detect_and_parse,
    resolve_import,
    watched_title,
)
from tindarr.swipe.imports.run import episode_totals

__all__ = [
    "GridService",
    "Household",
    "ImportService",
    "household",
    "import_in_progress",
    "import_too_large",
    "import_unreadable",
    "tmdb_not_configured",
]

logger = logging.getLogger(__name__)

#: The default region when the administrator has not set one. TMDb's own.
_DEFAULT_REGION: Final = "US"


def tmdb_not_configured() -> ProblemError:
    """409: nothing can be identified without TMDb."""
    return ProblemError(
        HTTPStatus.CONFLICT, "tmdb_not_configured", "Configure the TMDb connector first."
    )


def import_unreadable() -> ProblemError:
    """400: the upload is not a Netflix, IMDb or Letterboxd export.

    One answer for every reason. A caller learns that the file was not recognised, never
    how far a parser got into it, which is how a probe maps what it can smuggle past one.
    """
    return ProblemError(
        HTTPStatus.BAD_REQUEST,
        "import_unreadable",
        "This file is not a Netflix, IMDb or Letterboxd export we can read.",
    )


def import_too_large() -> ProblemError:
    """413: the upload is bigger than any real export.

    Checked against the declared length first and against the bytes as they arrive
    second, because a ``Content-Length`` is a claim and the body is the fact.
    """
    return ProblemError(
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        "import_too_large",
        "This file is larger than any export we accept.",
    )


def import_in_progress() -> ProblemError:
    """409: this user already has an import running, or the server has enough of them."""
    return ProblemError(
        HTTPStatus.CONFLICT, "import_in_progress", "An import is already running; wait for it."
    )


@dataclass(frozen=True, slots=True)
class Household:
    """What the server's settings mean to the swipe engine. Read, never cached."""

    language: str = "en"
    region: str = _DEFAULT_REGION
    filters: TitleFilters = field(default_factory=TitleFilters)


async def household(settings: SettingsStore) -> Household:
    """Read the language, the region and the content filters as the engine wants them."""
    language = (await settings.get("language")).value
    region = (await settings.get("streaming_region")).value
    raw = (await settings.get("content_filters")).value
    stored = ContentFilters.model_validate(raw) if isinstance(raw, dict) else ContentFilters()
    return Household(
        language=language if isinstance(language, str) and language else "en",
        region=region if isinstance(region, str) and region else _DEFAULT_REGION,
        filters=TitleFilters(
            exclude_adult=stored.exclude_adult,
            min_year=stored.min_year,
            excluded_genres=frozenset(stored.excluded_genres),
            excluded_original_languages=frozenset(stored.excluded_original_languages),
        ),
    )


class ImportService:
    """Starting an import, running it, and answering what it could not settle."""

    def __init__(
        self,
        engine: AsyncEngine,
        settings: SettingsStore,
        connectors: ConnectorService,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._connectors = connectors
        self._clock = clock

    async def metadata(self) -> Metadata:
        """Return the TMDb adapter, or refuse: nothing here works without it."""
        found = await self._connectors.metadata()
        if found is None:
            raise tmdb_not_configured()
        return found

    async def start(self, user_id: str, payload: bytes) -> tuple[ImportRecord, ParsedFile]:
        """Parse an upload and open an import for it, or refuse before anything is stored.

        The file is read **before** the row is created, so a file nobody can read leaves
        nothing behind, and the caller is told what it is rather than being handed a
        failed job to poll.
        """
        await self.metadata()
        try:
            parsed = detect_and_parse(payload)
        except UnreadableImportError:
            # The parser's own words say how far it got; the caller gets none of them.
            logger.info("an upload was not an export we recognise")
            raise import_unreadable() from None
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            if await import_repository.running_for_user(connection, user_id) is not None:
                raise import_in_progress()
            record = await import_repository.create(connection, user_id, parsed.format, now=now)
        logger.info(
            "an import was accepted",
            extra={"import_format": parsed.format, "titles": parsed.rows_kept},
        )
        return record, parsed

    async def run(self, record: ImportRecord, parsed: ParsedFile) -> None:
        """Identify a parsed file and write down what it says. Never raises.

        A failure closes the import with a problem **code** and nothing else: a message
        from a parser, or a line of somebody's file, has no business in a status a
        console renders and a log keeps.
        """
        try:
            metadata = await self.metadata()
            place = await household(self._settings)
            outcome = await resolve_import(
                parsed, metadata, language=place.language, now=self._clock.now()
            )
        except MetadataDownError:
            await self._fail(record, "metadata_unreachable")
            return
        except ProblemError as failure:
            await self._fail(record, failure.code)
            return
        await self._store(record, outcome)

    async def run_failed(self, record: ImportRecord) -> None:
        """Close an import that stopped for a reason nobody planned for."""
        await self._fail(record, "internal_error")

    async def _fail(self, record: ImportRecord, code: str) -> None:
        async with write_transaction(self._engine) as connection:
            await import_repository.finish(
                connection, record.id, status="failed", now=self._clock.now(), error_code=code
            )
        logger.info("an import stopped early", extra={"reason": code})

    async def _store(self, record: ImportRecord, outcome: ImportOutcome) -> None:
        now = self._clock.now()
        entries = [
            NewReviewEntry(
                query=entry.query,
                kind_hint=entry.kind_hint,
                episodes=entry.episodes,
                rating=entry.rating,
                last_watched_at=entry.last_watched_at,
                candidates=tuple(
                    ReviewCandidate(
                        kind=candidate.ref.kind,
                        tmdb_id=candidate.ref.tmdb_id,
                        title=candidate.title,
                        year=candidate.year,
                        poster_path=candidate.poster_path,
                        similarity=round(candidate.similarity, 3),
                    )
                    for candidate in entry.candidates
                ),
            )
            for entry in outcome.review
        ]
        async with write_transaction(self._engine) as connection:
            await history_repository.record(connection, record.user_id, outcome.watched, now=now)
            queued = await import_repository.queue_reviews(connection, record, entries, now=now)
            await import_repository.finish(
                connection,
                record.id,
                status="complete",
                now=now,
                rows_read=outcome.titles,
                skipped=dict(outcome.skipped),
                matched=len(outcome.watched),
                queued=queued,
            )
        logger.info(
            "an import finished",
            extra={"matched": len(outcome.watched), "queued": queued},
        )

    async def accept(self, user_id: str, entry_id: str, ref: TitleRef) -> WatchedTitle:
        """Answer one open question with one of the titles it offered.

        Only a candidate the entry itself carries is accepted. The row is the user's
        own, so a free-form id would be no more than untidy — but an endpoint that takes
        an arbitrary title and an arbitrary count is a writer, and this one is an answer.
        """
        now = self._clock.now()
        async with self._engine.connect() as connection:
            entry = await import_repository.get_review(connection, user_id, entry_id)
        if entry is None or entry.status != "pending":
            raise _no_such_entry()
        if ref not in {candidate.ref for candidate in entry.offered}:
            raise _not_offered()
        source = await self._source_of(user_id, entry.import_id)
        totals = await episode_totals(await self.metadata(), [ref]) if ref.kind == "tv" else {}
        row = watched_title(
            ref=ref,
            source=source,
            episodes=entry.episodes,
            episodes_total=totals.get(ref),
            rating=entry.rating,
            last_watched_at=entry.last_watched_at,
            now=now,
        )
        async with write_transaction(self._engine) as connection:
            if not await import_repository.decide(
                connection, user_id, entry_id, status="accepted", now=now
            ):
                raise _no_such_entry()
            await history_repository.record(connection, user_id, [row], now=now)
        return row

    async def reject(self, user_id: str, entry_id: str) -> None:
        """Answer one open question with "none of these"."""
        async with write_transaction(self._engine) as connection:
            if not await import_repository.decide(
                connection, user_id, entry_id, status="rejected", now=self._clock.now()
            ):
                raise _no_such_entry()

    async def forget(self, user_id: str, import_id: str) -> None:
        """Delete an import and everything that source told us about this user.

        Per source and per user: forgetting a Netflix import leaves the IMDb ratings and
        the calibration answers standing. It is the undo the console offers, and it is
        also what somebody who regrets uploading a file needs.
        """
        async with write_transaction(self._engine) as connection:
            record = await import_repository.get(connection, user_id, import_id)
            if record is None:
                raise _no_such_import()
            source = history_source_of(record)
            if source is not None:
                await history_repository.delete_source(connection, user_id, source)
            await connection.execute(
                import_repository.imports.delete()
                .where(import_repository.imports.c.id == import_id)
                .where(import_repository.imports.c.user_id == user_id)
            )

    async def _source_of(self, user_id: str, import_id: str) -> HistorySource:
        async with self._engine.connect() as connection:
            record = await import_repository.get(connection, user_id, import_id)
        source = history_source_of(record) if record is not None else None
        if source is None:  # pragma: no cover - the entry's own import row
            raise _no_such_entry()
        return source


class GridService:
    """Building a calibration wall for one household, and recording what they ticked."""

    def __init__(
        self,
        engine: AsyncEngine,
        settings: SettingsStore,
        connectors: ConnectorService,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._connectors = connectors
        self._clock = clock

    async def build(self, user_id: str, *, page: int, size: int) -> tuple[GridTitle, ...]:
        """Return the next wall of posters for this user."""
        metadata = await self._connectors.metadata()
        if metadata is None:
            raise tmdb_not_configured()
        place = await household(self._settings)
        async with self._engine.connect() as connection:
            answered = await history_repository.answered_refs(connection, user_id)
        return await CalibrationGrid(metadata).build(
            GridRequest(
                language=place.language,
                region=place.region,
                filters=place.filters,
                known=answered,
                page=page,
                size=size,
            )
        )

    async def submit(self, user_id: str, ticks: list[GridTick]) -> int:
        """Record a wall's answers; return how many rows were written."""
        now = self._clock.now()
        rows = grid_history(ticks, now=now)
        async with write_transaction(self._engine) as connection:
            return await history_repository.record(connection, user_id, rows, now=now)


def history_source_of(record: ImportRecord) -> HistorySource | None:
    """Return the history source an import row wrote under, narrowed from its column."""
    source = as_history_source(record.source)
    return source if source in IMPORT_SOURCES else None


def _no_such_entry() -> ProblemError:
    return ProblemError(HTTPStatus.NOT_FOUND, "not_found", "No such review entry.")


def _no_such_import() -> ProblemError:
    return ProblemError(HTTPStatus.NOT_FOUND, "not_found", "No such import.")


def _not_offered() -> ProblemError:
    return ProblemError(
        HTTPStatus.BAD_REQUEST, "validation_error", "title: not one of the offered candidates."
    )
