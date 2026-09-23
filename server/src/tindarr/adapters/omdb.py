"""OMDb: the three ratings TMDb does not carry (roadmap step 3).

OMDb is the optional half of the metadata port. A household without an OMDb key gets
cards without the IMDb, Rotten Tomatoes and Metacritic badges and nothing else changes,
so every failure here is a missing badge rather than a failed batch — which is why
``ratings`` answers ``None`` for a title OMDb does not know instead of raising.

Titles are looked up by **IMDb id only** (``?i=tt…``), never by title: OMDb's title
search is a guess, and a guess here would print another film's score on this one's card.
The id comes from TMDb, which is the authority.

Where the numbers live is not where a reader expects, and this is ported from the fork
rather than from OMDb's documentation:

- **IMDb** is the top-level ``imdbRating`` (``"7.8"``), not the ``Ratings`` entry;
- **Metacritic** is the top-level ``Metascore`` (``"81"``), not the ``Ratings`` entry,
  which spells it ``"81/100"``;
- **Rotten Tomatoes** exists only in ``Ratings``, as ``"94%"``.

Every one of them can be the string ``"N/A"``, which means "missing" and not zero.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

import httpx2

from tindarr.adapters.http import (
    NO_RESPONSE_REASONS,
    HttpSession,
    RemoteCallError,
    as_object_list,
    as_text,
    read_mapping,
)
from tindarr.ports.connectors import ConnectionCheck, ConnectorHealth
from tindarr.ports.metadata import Ratings

#: OMDb's only host. Like TMDb's, it is not configurable.
OMDB_BASE_URL: Final = "https://www.omdbapi.com"
#: Values OMDb uses for "I do not have this".
_MISSING: Final = frozenset({"", "N/A", "n/a"})
#: ``Ratings[].Source`` of the one rating that lives nowhere else.
_ROTTEN_TOMATOES: Final = "Rotten Tomatoes"
#: A film every OMDb key can read, used to tell a bad key from a bad address. Nothing
#: is done with the answer beyond "it parsed".
_PROBE_IMDB_ID: Final = "tt0111161"
#: IMDb ratings are 0-10; the two critics' scores are percentages.
_MAX_SCORE: Final = 10.0
_MAX_PERCENT: Final = 100

logger = logging.getLogger(__name__)


def _number(raw: object) -> str | None:
    """Return an OMDb field as text, unless it is one of its ways of saying nothing."""
    text = as_text(raw)
    return None if text is None or text.strip() in _MISSING else text.strip()


def rating_out_of_ten(raw: object) -> float | None:
    """Read ``imdbRating`` as a number on TMDb's own scale, or ``None``."""
    text = _number(raw)
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 0.0 <= value <= _MAX_SCORE else None


def percentage(raw: object) -> int | None:
    """Read a percentage (``"94%"``, ``"81"``) as an integer 0-100, or ``None``."""
    text = _number(raw)
    if text is None:
        return None
    text = text.removesuffix("%").strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if 0 <= value <= _MAX_PERCENT else None


def ratings_of(payload: Mapping[str, Any]) -> Ratings | None:
    """Turn one OMDb answer into ``Ratings``, or ``None`` when it knows no such title."""
    if (as_text(payload.get("Response")) or "").casefold() == "false":
        return None
    tomatoes: int | None = None
    for entry in as_object_list(payload.get("Ratings")):
        if as_text(entry.get("Source")) == _ROTTEN_TOMATOES:
            # Last one wins, as in the fork: OMDb has been seen listing a source twice,
            # and the later entry is the one its own page shows.
            tomatoes = percentage(entry.get("Value"))
    return Ratings(
        imdb=rating_out_of_ten(payload.get("imdbRating")),
        rotten_tomatoes=tomatoes,
        metacritic=percentage(payload.get("Metascore")),
    )


def _expect_ok(response: httpx2.Response) -> httpx2.Response:
    """Return the response, or fail when OMDb answered anything but ``200``."""
    if response.status_code != HTTPStatus.OK:
        raise RemoteCallError(f"status_{response.status_code}")
    return response


class OmdbRatings:
    """The ``RatingsSource`` port over OMDb."""

    def __init__(self, api_key: str, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport

    def _session(self) -> HttpSession:
        return HttpSession(
            OMDB_BASE_URL, headers={"Accept": "application/json"}, transport=self._transport
        )

    async def test(self) -> ConnectionCheck:
        """Check the key by asking for one film every OMDb key can read."""
        try:
            async with self._session() as session:
                response = await session.request_bounded(
                    "GET", "/", params={"apikey": self._api_key, "i": _PROBE_IMDB_ID}
                )
        except RemoteCallError as failure:
            return ConnectionCheck(self._health_of(failure), "OMDb")
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return ConnectionCheck("unauthorized", "OMDb")
        if response.status_code != HTTPStatus.OK:
            return ConnectionCheck("unexpected_response", "OMDb")
        try:
            payload = read_mapping(response)
        except RemoteCallError:
            return ConnectionCheck("unexpected_response", "OMDb")
        if (as_text(payload.get("Response")) or "").casefold() == "false":
            # OMDb answers ``200`` with ``Response: False`` for a key it refuses as well
            # as for a title it lacks; this title is one it always has.
            return ConnectionCheck("unauthorized", "OMDb")
        return ConnectionCheck("ok", "OMDb")

    async def ratings(self, imdb_id: str) -> Ratings | None:
        """Return the ratings for an IMDb id, or ``None`` when there are none.

        A failure is ``None`` too, logged and not raised: a card without a Rotten
        Tomatoes badge is a card, and a batch must not fail over a missing one.
        """
        if not imdb_id.startswith("tt"):
            return None
        try:
            async with self._session() as session:
                response = await session.request_bounded(
                    "GET", "/", params={"apikey": self._api_key, "i": imdb_id}
                )
                payload = read_mapping(_expect_ok(response))
        except RemoteCallError as failure:
            logger.info(
                "OMDb did not answer usably; the card keeps its TMDb rating only",
                extra={"metadata": "omdb", "reason": failure.reason},
            )
            return None
        found = ratings_of(payload)
        return None if found is None or found.empty else found

    @staticmethod
    def _health_of(failure: RemoteCallError) -> ConnectorHealth:
        return "unreachable" if failure.reason in NO_RESPONSE_REASONS else "unexpected_response"
