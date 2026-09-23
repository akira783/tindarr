"""Reading a Plex library and its watch history with the owner's token (roadmap step 3).

Plex is the one media server where "what has this person watched?" is not a question the
configured credential can fully answer. A Plex server keeps a separate view state per
account and serves the one belonging to the token that asked, so the stored **owner**
token sees the owner's progress and nobody else's. What it can see for everyone else is
the server's *history*: ``/status/sessions/history/all`` accepts an ``accountID`` filter
for the server's owner, and returns one row per viewing — what was played and when, but
never how far into it somebody got.

That gap is the whole of the decision recorded in
[ADR 0012](../../../../docs/adr/0012-plex-tokens.md), and this module is where it shows:

- **the owner** gets the same three states as a Jellyfin user, progress included, read
  from ``viewOffset`` / ``duration`` for a film and ``viewedLeafCount`` / ``leafCount``
  for a series;
- **everyone else** gets states derived from completed viewings only. A film in the
  history was watched; a series is counted by how many distinct episodes appear. Nothing
  is ever reported as ``in_progress`` or half-watched for them, because the owner token
  cannot know it — not because they never paused anything.

Deriving the second case from play *counts* was never an option: the history is a log of
viewings, and the port's contract is that a count of plays is not a signal
(``tindarr.ports.media_server.Engagement``).
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

from tindarr.adapters.http import as_object, as_text
from tindarr.ports.media_server import (
    Engagement,
    LibraryItem,
    film_engagement,
    series_engagement,
)
from tindarr.ports.titles import MediaKind

#: Rows asked for per page, through Plex's own container headers.
PAGE_SIZE: Final = 500
MAX_PAGES: Final = 200
#: How many history rows are read for one user. A year of heavy use is far below this.
HISTORY_LIMIT: Final = 5000
#: ``viewOffset`` and ``duration`` are milliseconds.
_MS: Final = 1000.0
#: The scheme of the only provider id that interests Tindarr.
TMDB_GUID_PREFIX: Final = "tmdb://"

logger = logging.getLogger(__name__)


def media_kind_of(plex_type: str | None) -> MediaKind | None:
    """Return the port's kind for a Plex ``type``, or ``None`` for anything else."""
    if plex_type == "movie":
        return "movie"
    if plex_type == "show":
        return "tv"
    return None


def tmdb_id_of(row: Mapping[str, Any]) -> int | None:
    """Read the TMDb id out of a Plex ``Guid`` list.

    Plex stores every agent's id side by side (``imdb://``, ``tvdb://``, ``tmdb://``) and
    sometimes decorates them (``tmdb://123?lang=en``). Only the digits before any
    decoration are kept, and only when they are digits: a guess here would attach the
    wrong film to a library entry.
    """
    guids: object = row.get("Guid")
    if not isinstance(guids, list):
        return None
    for value in cast("list[object]", guids):
        entry = as_object(value)
        raw = as_text(entry.get("id")) if entry is not None else None
        if raw is None or not raw.startswith(TMDB_GUID_PREFIX):
            continue
        digits = raw[len(TMDB_GUID_PREFIX) :].split("?", 1)[0].split("/", 1)[0].strip()
        if digits.isdigit() and int(digits) > 0:
            return int(digits)
    return None


def library_item(row: Mapping[str, Any]) -> LibraryItem | None:
    """Build a ``LibraryItem`` from one ``Metadata`` row, or ``None`` when it is neither kind."""
    kind = media_kind_of(as_text(row.get("type")))
    rating_key = as_text(row.get("ratingKey"))
    if kind is None or rating_key is None:
        return None
    year = row.get("year")
    return LibraryItem(
        kind=kind,
        item_id=rating_key,
        name=as_text(row.get("title")) or rating_key,
        tmdb_id=tmdb_id_of(row),
        year=year if isinstance(year, int) and not isinstance(year, bool) else None,
    )


def moment_of(value: object) -> datetime | None:
    """Read a Plex epoch-seconds timestamp (``lastViewedAt``, ``viewedAt``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _count(value: object) -> int:
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def days_since(moment: datetime | None, now: datetime) -> int | None:
    """Whole days between ``moment`` and ``now``; ``None`` when there is no date."""
    if moment is None:
        return None
    return max((now - moment).days, 0)


def owner_engagement(rows: Sequence[Mapping[str, Any]], now: datetime) -> list[Engagement]:
    """Build the owner's engagements from a library listing read with their own token."""
    return list(_owner_engagements(rows, now))


def _owner_engagements(rows: Sequence[Mapping[str, Any]], now: datetime) -> Iterator[Engagement]:
    for row in rows:
        item = library_item(row)
        if item is None:
            continue
        moment = moment_of(row.get("lastViewedAt"))
        engagement = (
            _owner_series(item, row, moment, now)
            if item.kind == "tv"
            else _owner_film(item, row, moment, now)
        )
        if engagement is not None:
            yield engagement


def _owner_film(
    item: LibraryItem, row: Mapping[str, Any], moment: datetime | None, now: datetime
) -> Engagement | None:
    # The one place in Tindarr that reads a play count, and only ever as a boolean:
    # Plex has no "played" flag, so "viewed at least once" is the only way to ask the
    # question every other server answers with one. How *many* times is not read here
    # and is not read anywhere (``tindarr.ports.media_server.Engagement``).
    played = _count(row.get("viewCount")) > 0
    offset, duration = _number(row.get("viewOffset")) or 0.0, _number(row.get("duration")) or 0.0
    progress = min(offset / duration, 1.0) if duration > 0 else 0.0
    if not played and progress <= 0.0:
        return None
    return Engagement(
        item=item,
        state=film_engagement(played=played, progress=progress, days_since=days_since(moment, now)),
        progress=1.0 if played else progress,
        last_played_at=moment,
    )


def _owner_series(
    item: LibraryItem, row: Mapping[str, Any], moment: datetime | None, now: datetime
) -> Engagement | None:
    played = _count(row.get("viewedLeafCount"))
    total = _count(row.get("leafCount")) or None
    if played == 0:
        return None
    return Engagement(
        item=item,
        state=series_engagement(
            episodes_played=played, episodes_total=total, days_since=days_since(moment, now)
        ),
        progress=played / total if total else 0.0,
        episodes_played=played,
        episodes_total=total,
        last_played_at=moment,
    )


class WatchHistory:
    """One other user's viewings, as the server's history reports them.

    Rows are keyed by the item a card would be about: a film by its own rating key, an
    episode by its series'. Episodes are counted **distinctly**, so re-watching the same
    one three times is one episode seen, not three.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._films: set[str] = set()
        self._episodes: dict[str, set[str]] = {}
        #: The most recent viewing per title; ``_films`` only answers "at all?".
        self._last: dict[str, datetime] = {}
        for row in rows:
            self._add(row)

    def _add(self, row: Mapping[str, Any]) -> None:
        kind, rating_key = as_text(row.get("type")), as_text(row.get("ratingKey"))
        moment = moment_of(row.get("viewedAt"))
        if kind == "movie" and rating_key is not None:
            self._films.add(rating_key)
            self._remember(rating_key, moment)
            return
        series_key = as_text(row.get("grandparentRatingKey"))
        if kind == "episode" and series_key is not None and rating_key is not None:
            self._episodes.setdefault(series_key, set()).add(rating_key)
            self._remember(series_key, moment)

    def _remember(self, key: str, moment: datetime | None) -> None:
        known = self._last.get(key)
        if moment is not None and (known is None or moment > known):
            self._last[key] = moment

    def engagements(self, library: Sequence[Mapping[str, Any]], now: datetime) -> list[Engagement]:
        """Turn the history into engagements, using the library for episode counts."""
        return list(self._engagements(library, now))

    def _engagements(
        self, library: Sequence[Mapping[str, Any]], now: datetime
    ) -> Iterator[Engagement]:
        for row in library:
            item = library_item(row)
            if item is None:
                continue
            moment = self._last.get(item.item_id)
            if item.kind == "movie":
                if item.item_id not in self._films:
                    continue
                # The history only records viewings, so a film in it was watched to the
                # end as far as this token can tell; there is no offset to read.
                yield Engagement(
                    item=item,
                    state=film_engagement(
                        played=True, progress=1.0, days_since=days_since(moment, now)
                    ),
                    progress=1.0,
                    last_played_at=moment,
                )
                continue
            played = len(self._episodes.get(item.item_id, ()))
            if played == 0:
                continue
            total = _count(row.get("leafCount")) or None
            yield Engagement(
                item=item,
                state=series_engagement(
                    episodes_played=played,
                    episodes_total=total,
                    days_since=days_since(moment, now),
                ),
                progress=played / total if total else 0.0,
                episodes_played=played,
                episodes_total=total,
                last_played_at=moment,
            )
