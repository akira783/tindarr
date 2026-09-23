"""Reading a Jellyfin or Emby library and one user's history (roadmap step 3).

Kept beside ``tindarr.adapters.mediabrowser`` rather than inside it because it answers a
different question — not "who is signing in?" but "what does this household own, and
what has this person done with it?" — and because it is where the two products differ
most in the small: the route that takes a user id moved in Jellyfin 10.9 and is gone in
12, while Emby still only has the old one.

Three decisions are worth reading before changing anything here.

**Paging is not optional.** The fork asks for five thousand items in one response. A
library of twenty thousand films and series, each carrying its provider ids, is several
megabytes, which the shared HTTP layer refuses outright (``MAX_RESPONSE_BYTES``). Every
listing here is therefore paged with ``StartIndex``, and a page that comes back shorter
than asked for ends the loop.

**Both route spellings are tried.** ``GET /Items?userId=…`` is the modern form and the
only one Jellyfin 12 has; ``GET /Users/{id}/Items`` is the old one and the only one Emby
has. The modern form is tried first and a ``404`` falls back once, per operation, which
costs one wasted call on Emby and keeps one code path for both.

**Play counts answer one question and no other.** ``UserData.PlayCount`` is in every
one of these responses, and it is used only to decide whether somebody ever opened a
title at all. It never reaches the classification: with remote or debrid playback each
restart increments it, so as a measure of *how much* was watched it measures the
network, not the viewer (``tindarr.ports.media_server.Engagement``). The fork draws the
line in the same place.
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, Final

import httpx2

from tindarr.adapters.http import (
    HttpSession,
    RemoteCallError,
    as_flag,
    as_object,
    as_object_list,
    as_text,
    read_mapping,
)
from tindarr.ports.media_server import (
    Engagement,
    LibraryItem,
    film_engagement,
    series_engagement,
)
from tindarr.ports.titles import MediaKind

#: Items asked for per page. Small enough that a page stays far below the body cap,
#: large enough that a family library is two or three calls.
PAGE_SIZE: Final = 500
#: Pages read before giving up, so a server whose ``StartIndex`` does nothing cannot
#: keep the loop running for ever. Two hundred pages is a hundred thousand items.
MAX_PAGES: Final = 200
#: How many "continue watching" entries are read. Both products cap this list anyway.
RESUME_LIMIT: Final = 200
#: The two item types a card can ever be.
LIBRARY_TYPES: Final = "Movie,Series"
#: Percentages come back 0-100; the port works in fractions.
PERCENT: Final = 100.0

logger = logging.getLogger(__name__)


def media_kind_of(item_type: str | None) -> MediaKind | None:
    """Return the port's kind for a Media Browser ``Type``, or ``None`` for anything else."""
    if item_type == "Movie":
        return "movie"
    if item_type == "Series":
        return "tv"
    return None


def tmdb_id_of(row: Mapping[str, Any]) -> int | None:
    """Read ``ProviderIds.Tmdb`` as a positive integer, or ``None``.

    Both products store it as a string, and both have been seen storing an empty one.
    Anything that is not a plain positive number is dropped rather than guessed at: a
    wrong id would put somebody else's film in the library index.
    """
    providers = as_object(row.get("ProviderIds")) or {}
    raw = as_text(providers.get("Tmdb")) or as_text(providers.get("tmdb"))
    if raw is None or not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def parse_media_date(value: object) -> datetime | None:
    """Parse a Media Browser timestamp, tolerating its seven fractional digits.

    Both products serialise .NET ``DateTime`` values, which carry one more digit of
    precision than ``datetime.fromisoformat`` accepts. A value without a zone is read as
    UTC, which is what both products send.
    """
    text = as_text(value)
    if text is None:
        return None
    text = text.replace("Z", "+00:00")
    head, dot, tail = text.partition(".")
    if dot:
        digits = "".join(character for character in tail if character.isdigit())
        text = f"{head}.{digits[:6]}{tail[len(digits) :]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def days_since(moment: datetime | None, now: datetime) -> int | None:
    """Whole days between ``moment`` and ``now``; ``None`` when there is no date."""
    if moment is None:
        return None
    return max((now - moment).days, 0)


def fraction(value: object) -> float:
    """Read a ``PlayedPercentage`` (0-100) as a fraction, clamped to 0-1."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return min(max(float(value) / PERCENT, 0.0), 1.0)


def library_item(row: Mapping[str, Any]) -> LibraryItem | None:
    """Build a ``LibraryItem`` from one listing row, or ``None`` when it is neither kind."""
    kind = media_kind_of(as_text(row.get("Type")))
    item_id = as_text(row.get("Id"))
    if kind is None or item_id is None:
        return None
    year = row.get("ProductionYear")
    return LibraryItem(
        kind=kind,
        item_id=item_id,
        name=as_text(row.get("Name")) or item_id,
        tmdb_id=tmdb_id_of(row),
        year=year if isinstance(year, int) and not isinstance(year, bool) else None,
    )


def rows_of(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the ``Items`` of a listing response, skipping anything that is not an object."""
    if not isinstance(payload.get("Items"), list):
        raise RemoteCallError("unexpected_response")
    return as_object_list(payload.get("Items"))


class UserItemsReader:
    """Pages through the listings of one Media Browser server, old route or new.

    One instance per operation: it remembers which of the two spellings answered, so the
    fallback costs one call rather than one per page.
    """

    def __init__(self, session: HttpSession, user_id: str | None = None) -> None:
        self._session = session
        self._user_id = user_id
        self._legacy = False

    async def page(self, suffix: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        """Read one listing page, falling back to the pre-10.9 route once."""
        if not self._legacy:
            response = await self._session.request(
                "GET", self._modern(suffix), params=self._with_user(params)
            )
            if response.status_code != HTTPStatus.NOT_FOUND or self._user_id is None:
                return read_mapping(_expect_ok(response))
            self._legacy = True
        response = await self._session.request("GET", self._legacy_path(suffix), params=params)
        return read_mapping(_expect_ok(response))

    def _modern(self, suffix: str) -> str:
        return "/UserItems/Resume" if suffix == "/Resume" else "/Items"

    def _legacy_path(self, suffix: str) -> str:
        return f"/Users/{self._user_id}/Items{suffix}"

    def _with_user(self, params: Mapping[str, str]) -> Mapping[str, str]:
        if self._user_id is None:
            return params
        return dict(params) | {"userId": self._user_id}

    async def all_rows(self, params: Mapping[str, str]) -> list[Mapping[str, Any]]:
        """Read every page of a listing, in order."""
        collected: list[Mapping[str, Any]] = []
        for page in range(MAX_PAGES):
            payload = await self.page(
                "", dict(params) | {"Limit": str(PAGE_SIZE), "StartIndex": str(page * PAGE_SIZE)}
            )
            rows = rows_of(payload)
            collected.extend(rows)
            if len(rows) < PAGE_SIZE:
                return collected
        logger.warning(
            "stopped reading the media server library at the page cap",
            extra={"pages": MAX_PAGES, "items": len(collected)},
        )
        return collected


def _expect_ok(response: httpx2.Response) -> httpx2.Response:
    if response.status_code != HTTPStatus.OK:
        raise RemoteCallError(f"status_{response.status_code}")
    return response


LIBRARY_PARAMS: Final[Mapping[str, str]] = {
    "Recursive": "true",
    "IncludeItemTypes": LIBRARY_TYPES,
    "Fields": "ProviderIds,ProductionYear",
    "EnableUserData": "false",
    "EnableImages": "false",
    "SortBy": "SortName",
    "SortOrder": "Ascending",
}
ENGAGEMENT_PARAMS: Final[Mapping[str, str]] = {
    "Recursive": "true",
    "IncludeItemTypes": LIBRARY_TYPES,
    "Fields": "ProviderIds,ProductionYear",
    "EnableUserData": "true",
    "EnableImages": "false",
    "SortBy": "SortName",
    "SortOrder": "Ascending",
}
PLAYED_EPISODE_PARAMS: Final[Mapping[str, str]] = {
    "Recursive": "true",
    "IncludeItemTypes": "Episode",
    "IsPlayed": "true",
    "EnableUserData": "true",
    "EnableImages": "false",
}


class EpisodeTally:
    """How many episodes of each series a user has played, and when they last did."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._played: dict[str, int] = {}
        self._last: dict[str, datetime] = {}
        for row in rows:
            series_id = as_text(row.get("SeriesId"))
            if series_id is None:
                continue
            self._played[series_id] = self._played.get(series_id, 0) + 1
            user_data = as_object(row.get("UserData")) or {}
            moment = parse_media_date(user_data.get("LastPlayedDate"))
            known = self._last.get(series_id)
            if moment is not None and (known is None or moment > known):
                self._last[series_id] = moment

    def played(self, series_id: str) -> int:
        """Episodes of that series the user has played."""
        return self._played.get(series_id, 0)

    def last_played(self, series_id: str) -> datetime | None:
        """When the user last played an episode of that series."""
        return self._last.get(series_id)


class ResumeList:
    """The "continue watching" list, keyed by the item a card would be about.

    An episode in progress is progress on its **series**, which is what a card offers,
    so it is filed under ``SeriesId``.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._progress: dict[str, float] = {}
        self._last: dict[str, datetime | None] = {}
        for row in rows:
            key = as_text(row.get("SeriesId")) or as_text(row.get("Id"))
            if key is None:
                continue
            user_data = as_object(row.get("UserData")) or {}
            self._progress[key] = max(
                self._progress.get(key, 0.0), fraction(user_data.get("PlayedPercentage"))
            )
            self._last.setdefault(key, parse_media_date(user_data.get("LastPlayedDate")))

    def __contains__(self, item_id: str) -> bool:
        """Whether the user has this item part-watched."""
        return item_id in self._progress

    def progress(self, item_id: str) -> float:
        """How far into the item the user got, as a fraction."""
        return self._progress.get(item_id, 0.0)

    def last_played(self, item_id: str) -> datetime | None:
        """When they last did."""
        return self._last.get(item_id)


def latest(*moments: datetime | None) -> datetime | None:
    """Return the most recent of several timestamps, ignoring the missing ones."""
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else None


def build_engagements(
    titles: Sequence[Mapping[str, Any]],
    episodes: EpisodeTally,
    resume: ResumeList,
    now: datetime,
) -> list[Engagement]:
    """Turn three listings into one engagement per title the user has actually touched.

    A title nobody has opened produces nothing: "never watched" is the default for every
    title in the world and would drown the few that mean something.
    """
    return list(_engagements(titles, episodes, resume, now))


def _engagements(
    titles: Sequence[Mapping[str, Any]],
    episodes: EpisodeTally,
    resume: ResumeList,
    now: datetime,
) -> Iterator[Engagement]:
    for row in titles:
        item = library_item(row)
        if item is None:
            continue
        user_data = as_object(row.get("UserData")) or {}
        engagement = (
            _series(item, row, user_data, episodes, resume, now)
            if item.kind == "tv"
            else _film(item, user_data, resume, now)
        )
        if engagement is not None:
            yield engagement


def touched(user_data: Mapping[str, Any]) -> bool:
    """Whether this user ever opened the title, by the one fact that always says so.

    A play count above zero is that fact and nothing more: it tells the reader the title
    was started, never how thoroughly. Dropping it here would lose every title somebody
    played on a client that reports no percentage.
    """
    count = user_data.get("PlayCount")
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def _film(
    item: LibraryItem,
    user_data: Mapping[str, Any],
    resume: ResumeList,
    now: datetime,
) -> Engagement | None:
    played = as_flag(user_data.get("Played"), default=False)
    # The resume list is capped by both products, so a long-abandoned film may only have
    # its own ``PlayedPercentage`` left: the larger of the two is the honest number.
    progress = max(fraction(user_data.get("PlayedPercentage")), resume.progress(item.item_id))
    if not played and progress <= 0.0 and not touched(user_data):
        return None
    moment = latest(
        parse_media_date(user_data.get("LastPlayedDate")), resume.last_played(item.item_id)
    )
    return Engagement(
        item=item,
        state=film_engagement(played=played, progress=progress, days_since=days_since(moment, now)),
        progress=1.0 if played else progress,
        last_played_at=moment,
    )


def _series(  # noqa: PLR0913, PLR0917 - one argument per listing this is built from
    item: LibraryItem,
    row: Mapping[str, Any],
    user_data: Mapping[str, Any],
    episodes: EpisodeTally,
    resume: ResumeList,
    now: datetime,
) -> Engagement | None:
    played = episodes.played(item.item_id)
    in_progress = item.item_id in resume
    if played == 0 and not in_progress and not touched(user_data):
        return None
    unplayed = user_data.get("UnplayedItemCount")
    total = (
        played + unplayed
        if isinstance(unplayed, int) and not isinstance(unplayed, bool) and unplayed >= 0
        else None
    )
    moment = latest(
        parse_media_date(user_data.get("LastPlayedDate")),
        episodes.last_played(item.item_id),
        resume.last_played(item.item_id),
    )
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
