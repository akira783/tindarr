"""One import, from parsed rows to history rows and a review queue.

Three things happen here and they are worth naming separately, because the value of an
import is unevenly spread between them.

**Identification** (``tindarr.swipe.imports.resolve``) turns a line into a TMDb id or
into a question. **Merging** puts the rows that turned out to be the same title back
together: a Netflix history spells one series four ways across its seasons, and an IMDb
export rates its episodes one at a time, so *Overlord*, *Overlord: Overlord III* and
fourteen episode ids are one title somebody watched fifteen episodes of, not fifteen
titles. **Engagement** is what ADR 0013 says the whole exercise is actually for:
counting those fifteen against the fifty-two TMDb knows about turns a list of evenings
into "started it and stopped", which is a taste signal no other source carries.

The thresholds are **not** this module's. ``series_engagement`` and ``film_engagement``
come from the media server port, unchanged, so a series somebody abandoned on Netflix
and one they abandoned on Jellyfin are the same word to the engine. A definition of
"gave up on it" that lived in two places would be two decks disagreeing about the same
evening.

**A film row is a viewing.** Netflix writes a row once somebody has really watched
something, and a rating is somebody saying they saw it, so an identified film is
``watched``. Nothing here invents a progress fraction it was not told.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import film_engagement, series_engagement
from tindarr.ports.metadata import Metadata
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.history import HistorySource, WatchedTitle
from tindarr.swipe.imports.records import ImportFormat, ParsedFile, WatchedItem
from tindarr.swipe.imports.resolve import Candidate, MetadataDownError, Resolution, TitleResolver

__all__ = [
    "ImportOutcome",
    "MetadataDownError",
    "ReviewEntry",
    "episode_totals",
    "resolve_import",
    "watched_title",
]

logger = logging.getLogger(__name__)

#: A day, for turning "last watched" into the media server port's ``days_since``.
_SECONDS_PER_DAY: Final = 86_400


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    """One row the import refused to guess at, and what it would have guessed.

    ``candidates`` can be empty: TMDb knew nothing that resembled the row. It is still
    queued, because "we found nothing for *Emergência Radioativa*" is something the
    person who uploaded the file can act on and a silent drop is not.
    """

    query: str
    kind_hint: MediaKind | None = None
    episodes: int = 0
    rating: float | None = None
    last_watched_at: datetime | None = None
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """Everything one file produced."""

    format: ImportFormat
    watched: tuple[WatchedTitle, ...] = ()
    review: tuple[ReviewEntry, ...] = ()
    skipped: Mapping[str, int] = field(default_factory=dict[str, int])
    #: Distinct titles the file held, however they ended up.
    titles: int = 0


@dataclass(slots=True)
class _Merged:
    """Several rows that turned out to be one title."""

    ref: TitleRef
    episodes: int = 0
    rating: float | None = None
    last_watched_at: datetime | None = None

    def absorb(self, item: WatchedItem, *, episode: bool) -> None:
        """Fold one more row of the same title in."""
        self.episodes += item.episodes if not episode else max(item.episodes, 1)
        if item.rating is not None:
            self.rating = item.rating if self.rating is None else max(self.rating, item.rating)
        when = item.last_watched_at
        if when is not None and (self.last_watched_at is None or when > self.last_watched_at):
            self.last_watched_at = when


async def resolve_import(
    parsed: ParsedFile,
    metadata: Metadata,
    *,
    language: str,
    now: datetime,
) -> ImportOutcome:
    """Identify a parsed file against TMDb and turn it into history and questions.

    Raises ``MetadataDownError`` when TMDb has refused several searches in a row: the
    rest of the file would become review entries about a service being down, which is a
    queue nobody can answer.
    """
    resolver = TitleResolver(metadata, language)
    merged: dict[TitleRef, _Merged] = {}
    review: list[ReviewEntry] = []
    for item in parsed.items:
        resolution = await resolver.resolve(item)
        if resolution.confident and resolution.best is not None:
            entry = merged.setdefault(resolution.best.ref, _Merged(resolution.best.ref))
            entry.absorb(item, episode=resolution.episode)
        else:
            review.append(_review_entry(resolution))
    source = _source(parsed.format)
    watched = await _history(metadata, list(merged.values()), source=source, now=now)
    return ImportOutcome(
        format=parsed.format,
        watched=watched,
        review=tuple(review),
        skipped=dict(parsed.skipped),
        titles=len(parsed.items),
    )


def _source(kind: ImportFormat) -> HistorySource:
    """Every import format is also a history source, by the same name."""
    source: HistorySource = kind
    return source


def _review_entry(resolution: Resolution) -> ReviewEntry:
    item = resolution.item
    best = resolution.best
    offered: tuple[Candidate, ...] = (best, *resolution.alternatives) if best is not None else ()
    return ReviewEntry(
        query=item.query,
        kind_hint=item.kind_hint,
        episodes=item.episodes,
        rating=item.rating,
        last_watched_at=item.last_watched_at,
        candidates=offered,
    )


async def _history(
    metadata: Metadata,
    merged: Sequence[_Merged],
    *,
    source: HistorySource,
    now: datetime,
) -> tuple[WatchedTitle, ...]:
    entries = list(merged)
    totals = await episode_totals(
        metadata, [entry.ref for entry in entries if entry.ref.kind == "tv"]
    )
    return tuple(
        watched_title(
            ref=entry.ref,
            source=source,
            episodes=entry.episodes,
            episodes_total=totals.get(entry.ref),
            rating=entry.rating,
            last_watched_at=entry.last_watched_at,
            now=now,
        )
        for entry in entries
    )


async def episode_totals(
    metadata: Metadata, refs: Sequence[TitleRef], language: str = "en"
) -> dict[TitleRef, int | None]:
    """Read how many episodes each series has, tolerating the ones TMDb will not say.

    A series whose total is unknown is judged on recency alone by ``series_engagement``
    and never on a ratio, which is the port's own rule: an unknown denominator must not
    become a confident verdict.
    """
    totals: dict[TitleRef, int | None] = {}
    for ref in dict.fromkeys(refs):
        try:
            details = await metadata.details(ref, language)
        except ProblemError:
            logger.info("TMDb would not say how many episodes an imported series has")
            totals[ref] = None
            continue
        totals[ref] = details.episodes
    return totals


def watched_title(  # noqa: PLR0913 - one keyword per fact a source can carry
    *,
    ref: TitleRef,
    source: HistorySource,
    episodes: int = 0,
    episodes_total: int | None = None,
    rating: float | None = None,
    last_watched_at: datetime | None = None,
    now: datetime,
) -> WatchedTitle:
    """Build one history row, with the media server port's own engagement rules.

    Shared by the import run and by accepting a review entry, so a title somebody had to
    confirm by hand gets exactly the verdict it would have got automatically.
    """
    days = _days_since(last_watched_at, now)
    if ref.kind == "tv" and episodes > 0:
        state = series_engagement(
            episodes_played=episodes, episodes_total=episodes_total, days_since=days
        )
        progress = episodes / episodes_total if episodes_total else 0.0
        return WatchedTitle(
            ref=ref,
            source=source,
            state=state,
            progress=min(progress, 1.0),
            episodes_played=episodes,
            episodes_total=episodes_total,
            rating=rating,
            last_watched_at=last_watched_at,
        )
    # A film, or a series a source counted no episodes for: the row itself is the
    # evidence that it was watched, and nothing here invents a fraction.
    return WatchedTitle(
        ref=ref,
        source=source,
        state=film_engagement(played=True, progress=1.0, days_since=days),
        progress=1.0,
        rating=rating,
        last_watched_at=last_watched_at,
    )


def _days_since(when: datetime | None, now: datetime) -> int | None:
    if when is None:
        return None
    return max(int((now - when).total_seconds() // _SECONDS_PER_DAY), 0)
