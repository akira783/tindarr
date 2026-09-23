"""TMDb: the authority every card is built from (roadmap step 3).

A suggestion from an AI provider is a string until TMDb has turned it into an id, so
this adapter is the narrow place where untrusted text becomes a ``TitleRef``. What comes
back out is data and only data: image **paths**, never URLs the server would fetch, and
a trailer as a YouTube key, never a link (the security model, section 6). The API
prefixes paths with TMDb's allow-listed base; nothing here builds one.

**Two credentials, one field.** TMDb hands out a v3 API key (32 hexadecimal characters,
passed as a query parameter) and a v4 read access token (a JWT, passed as a bearer
header). Administrators copy whichever their account page showed them, so the shape of
the value decides how it travels, and a bearer token never reaches a query string.

**What is kept from the fork**, because it was measured against real answers rather
than guessed:

- the two year parameters are different (``year`` for films, ``first_air_date_year``
  for series) and a search that finds nothing is retried without the year;
- a title whose translation is empty keeps its English text rather than showing a blank
  overview;
- the trailer ranking — a trailer beats a teaser, official beats unofficial, the user's
  language beats English beats everything else — and nothing about video resolution;
- the watch providers of one region are read out of the whole answer, because TMDb
  returns every region at once and takes no region parameter.

**What is not kept**: the fork takes ``results[0]`` and calls it a match. That is the
fallback here, not the rule: an exact title in the right year wins over a more popular
near-miss, which is what stops "Dune" (1984) being served for a 2021 suggestion.
"""

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final, cast

import httpx2

from tindarr.adapters.http import (
    NO_RESPONSE_REASONS,
    HttpSession,
    RemoteCallError,
    as_object,
    as_object_list,
    as_text,
    read_mapping,
)
from tindarr.ports import problems
from tindarr.ports.connectors import ConnectionCheck, ConnectorHealth
from tindarr.ports.metadata import (
    OfferKind,
    Provider,
    SearchQuery,
    Title,
    TitleDetails,
    TitleFilters,
    Trailer,
)
from tindarr.ports.titles import MediaKind, TitleRef

#: TMDb's own v3 base. It is not configurable: there is one TMDb.
TMDB_BASE_URL: Final = "https://api.themoviedb.org/3"
#: The language every title falls back to; TMDb always has it.
FALLBACK_LANGUAGE: Final = "en"
#: TMDb's offer buckets, mapped to the contract's words, best first. The order is also
#: the priority when one provider appears in several buckets.
OFFER_BUCKETS: Final[tuple[tuple[str, OfferKind], ...]] = (
    ("flatrate", "subscription"),
    ("free", "free"),
    ("ads", "ads"),
    ("rent", "rent"),
    ("buy", "buy"),
)
#: Videos worth showing, best kind first.
_TRAILER_TYPES: Final = ("Trailer", "Teaser")
#: The only video host Tindarr will point the app at.
_YOUTUBE: Final = "YouTube"
#: A YouTube video id, as the contract's ``Trailer.key`` defines it. A key that does
#: not look like one is dropped rather than passed on: the app turns it into a
#: ``youtube-nocookie.com`` URL, so it is a value that becomes part of a link.
_YOUTUBE_KEY: Final = re.compile(r"[A-Za-z0-9_-]{6,20}")
#: An image path as TMDb writes them: a leading slash and a plain file name. Anything
#: else is dropped, because the API turns a path into a URL under TMDb's image host and
#: a path with ``..`` or a scheme in it would not stay under it.
_IMAGE_PATH: Final = re.compile(r"/[A-Za-z0-9._-]{1,128}")
#: A v4 read access token is a JWT; a v3 key is not.
_BEARER_PREFIX: Final = "eyJ"
_JWT_PARTS: Final = 3
#: How many digits a year has, in a date and in a title's trailing "(2016)".
_YEAR_DIGITS: Final = 4
#: TMDb's own "sort me last" value for a provider with no priority.
_LAST_PRIORITY: Final = 999

logger = logging.getLogger(__name__)


def normalize_title(value: str) -> str:
    """Return a title reduced to what two spellings of it have in common.

    Case, accents, punctuation and a trailing year in brackets all differ between what a
    model writes and what TMDb stores, and none of them distinguishes two real titles.
    """
    text = unicodedata.normalize("NFKD", value).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    kept = [character if character.isalnum() else " " for character in text]
    words = "".join(kept).split()
    if len(words) > 1 and words[-1].isdigit() and len(words[-1]) == _YEAR_DIGITS:
        words = words[:-1]
    return " ".join(words)


def image_path(value: object) -> str | None:
    """Return a TMDb image path, or ``None`` when it is not one.

    The API prefixes these with TMDb's allow-listed image base (the security model,
    section 6), so what is stored has to be a path and nothing else: an absolute URL or
    a ``..`` here would walk straight out of that base.
    """
    text = as_text(value)
    return text if text is not None and _IMAGE_PATH.fullmatch(text) else None


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _year_of(value: object) -> int | None:
    """Read the year out of a TMDb date (``2016-11-11``), or ``None``."""
    text = as_text(value)
    if text is None or len(text) < _YEAR_DIGITS or not text[:_YEAR_DIGITS].isdigit():
        return None
    return int(text[:_YEAR_DIGITS])


def _genre_ids(row: Mapping[str, Any]) -> tuple[int, ...]:
    """Read ``genre_ids`` (list endpoints) or ``genres`` (detail endpoints)."""
    raw = row.get("genre_ids")
    if isinstance(raw, list):
        ids = [_int(item) for item in cast("list[object]", raw)]
        return tuple(value for value in ids if value is not None)
    found = [_int(entry.get("id")) for entry in as_object_list(row.get("genres"))]
    return tuple(value for value in found if value is not None)


def _rows(payload: Mapping[str, Any], key: str = "results") -> list[Mapping[str, Any]]:
    return as_object_list(payload.get(key))


def title_of(row: Mapping[str, Any], kind: MediaKind) -> Title | None:
    """Build a ``Title`` from a search result, or ``None`` when it is unusable."""
    tmdb_id = _int(row.get("id"))
    name = as_text(row.get("title") if kind == "movie" else row.get("name"))
    if tmdb_id is None or tmdb_id <= 0 or name is None:
        return None
    date = row.get("release_date") if kind == "movie" else row.get("first_air_date")
    return Title(
        ref=TitleRef(kind, tmdb_id),
        title=name,
        original_title=as_text(
            row.get("original_title") if kind == "movie" else row.get("original_name")
        ),
        year=_year_of(date),
        overview=as_text(row.get("overview")),
        poster_path=image_path(row.get("poster_path")),
        original_language=as_text(row.get("original_language")),
        popularity=_float(row.get("popularity")) or 0.0,
        vote_average=_float(row.get("vote_average")),
        vote_count=_int(row.get("vote_count")) or 0,
        adult=row.get("adult") is True,
        genre_ids=_genre_ids(row),
    )


@dataclass(frozen=True, slots=True)
class _Score:
    """How well one candidate answers a search, biggest first."""

    title_matches: bool
    year_matches: bool
    year_distance: int
    popularity: float

    @property
    def key(self) -> tuple[int, int, int, float]:
        """A sort key: title, then year, then how far off the year is, then popularity."""
        return (
            int(self.title_matches),
            int(self.year_matches),
            -self.year_distance,
            self.popularity,
        )


class TmdbMetadata:
    """The ``Metadata`` port over TMDb's v3 API."""

    def __init__(self, api_key: str, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport
        #: ``normalised genre name -> TMDb id``, read once per adapter instance.
        self._genres: dict[str, int] | None = None

    # --- plumbing -------------------------------------------------------------------

    @property
    def _is_bearer(self) -> bool:
        return (
            self._api_key.startswith(_BEARER_PREFIX) and self._api_key.count(".") == _JWT_PARTS - 1
        )

    def _session(self) -> HttpSession:
        headers = {"Accept": "application/json"}
        if self._is_bearer:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return HttpSession(TMDB_BASE_URL, headers=headers, transport=self._transport)

    def _params(self, params: Mapping[str, str] | None = None) -> dict[str, str]:
        """Add the v3 key to a query, unless the credential is a bearer token."""
        query = dict(params or {})
        if not self._is_bearer:
            query["api_key"] = self._api_key
        return query

    async def _get(
        self, session: HttpSession, path: str, params: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        response = await session.request_bounded("GET", path, params=self._params(params))
        if response.status_code != HTTPStatus.OK:
            raise RemoteCallError(f"status_{response.status_code}")
        return read_mapping(response)

    @staticmethod
    def _unreachable(operation: str, failure: RemoteCallError) -> Exception:
        logger.warning(
            "TMDb did not answer usably",
            extra={"metadata": "tmdb", "operation": operation, "reason": failure.reason},
        )
        return problems.metadata_unreachable()

    # --- the connection test --------------------------------------------------------

    async def test(self) -> ConnectionCheck:
        """Check the key against ``/configuration``, the cheapest call that needs one."""
        try:
            async with self._session() as session:
                response = await session.request_bounded(
                    "GET", "/configuration", params=self._params()
                )
        except RemoteCallError as failure:
            return ConnectionCheck(self._health_of(failure), "TMDb")
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return ConnectionCheck("unauthorized", "TMDb")
        if response.status_code != HTTPStatus.OK:
            return ConnectionCheck("unexpected_response", "TMDb")
        try:
            read_mapping(response)
        except RemoteCallError:
            return ConnectionCheck("unexpected_response", "TMDb")
        return ConnectionCheck("ok", "TMDb")

    @staticmethod
    def _health_of(failure: RemoteCallError) -> ConnectorHealth:
        return "unreachable" if failure.reason in NO_RESPONSE_REASONS else "unexpected_response"

    # --- search ---------------------------------------------------------------------

    async def search(self, query: SearchQuery) -> list[Title]:
        """Search TMDb for a suggestion, retrying without the year when nothing matches.

        A model that gets the year wrong by one is common; a model that invents a title
        is not. Dropping the year is therefore the one retry worth making, and it is the
        fork's behaviour.
        """
        try:
            async with self._session() as session:
                found = await self._search_once(session, query, with_year=True)
                if not found and query.year is not None:
                    found = await self._search_once(session, query, with_year=False)
                for alias in query.aliases:
                    if found:
                        break
                    found = await self._search_once(session, query, with_year=False, title=alias)
        except RemoteCallError as failure:
            raise self._unreachable("search", failure) from None
        return found

    async def _search_once(
        self,
        session: HttpSession,
        query: SearchQuery,
        *,
        with_year: bool,
        title: str | None = None,
    ) -> list[Title]:
        params = {
            "query": title or query.title,
            "language": query.language,
            "include_adult": "true" if query.include_adult else "false",
            "page": "1",
        }
        if with_year and query.year is not None:
            params["year" if query.kind == "movie" else "first_air_date_year"] = str(query.year)
        payload = await self._get(session, f"/search/{query.kind}", params)
        return [found for row in _rows(payload) if (found := title_of(row, query.kind)) is not None]

    async def match(self, query: SearchQuery) -> Title | None:
        """Return the one title a suggestion means, filtered, or ``None``."""
        candidates = await self.search(query)
        if query.filters is not None:
            allowed = await self.excluded_genre_ids(query.filters)
            candidates = [
                candidate for candidate in candidates if passes(candidate, query.filters, allowed)
            ]
        return self.best(candidates, query)

    @staticmethod
    def best(candidates: Sequence[Title], query: SearchQuery) -> Title | None:
        """Pick the candidate that answers ``query``, TMDb's own order breaking ties."""
        if not candidates:
            return None
        wanted = normalize_title(query.title)
        scored = [
            (_score(candidate, wanted, query.year).key, -position, candidate)
            for position, candidate in enumerate(candidates)
        ]
        return max(scored, key=lambda entry: (entry[0], entry[1]))[2]

    # --- details --------------------------------------------------------------------

    async def details(self, ref: TitleRef, language: str) -> TitleDetails:
        """Read one title, filling an empty translated overview from English.

        TMDb answers a language it has no translation for with the original title and an
        empty overview rather than an error, so the second call is made only when the
        first came back without prose, and never when English was what was asked for.
        """
        try:
            async with self._session() as session:
                payload = await self._details_call(session, ref, language)
                overview = as_text(payload.get("overview"))
                if overview is None and _base_language(language) != FALLBACK_LANGUAGE:
                    fallback = await self._details_call(session, ref, FALLBACK_LANGUAGE)
                    payload = dict(payload) | {
                        "overview": fallback.get("overview"),
                        "tagline": payload.get("tagline") or fallback.get("tagline"),
                    }
        except RemoteCallError as failure:
            raise self._unreachable("details", failure) from None
        return _details_of(payload, ref)

    async def _details_call(
        self, session: HttpSession, ref: TitleRef, language: str
    ) -> Mapping[str, Any]:
        params = {"language": language}
        if ref.kind == "tv":
            # Films carry ``imdb_id`` already; series only have it in their external ids.
            params["append_to_response"] = "external_ids"
        return await self._get(session, f"/{ref.kind}/{ref.tmdb_id}", params)

    # --- watch providers ------------------------------------------------------------

    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]:
        """Where a title can be streamed in one region, best offer per provider.

        TMDb returns every region in one answer and takes no region parameter, so the
        region is applied here. A provider that offers a title both on subscription and
        for rent is listed once, as a subscription: the badge asks "is this included in
        something I already pay for?", and the better answer is the true one.
        """
        try:
            async with self._session() as session:
                payload = await self._get(session, f"/{ref.kind}/{ref.tmdb_id}/watch/providers")
        except RemoteCallError as failure:
            raise self._unreachable("watch_providers", failure) from None
        results = as_object(payload.get("results")) or {}
        region_data = as_object(results.get(region.upper())) or {}
        return _providers_of(region_data)

    async def region_providers(self, region: str) -> list[Provider]:
        """Every streaming provider available in a region, for a user's preferences."""
        try:
            async with self._session() as session:
                payload = await self._get(
                    session, "/watch/providers/movie", {"watch_region": region.upper()}
                )
        except RemoteCallError as failure:
            raise self._unreachable("region_providers", failure) from None
        found = [
            provider
            for row in _rows(payload)
            if (provider := _provider_of(row, "subscription")) is not None
        ]
        found.sort(key=lambda provider: provider.name.casefold())
        return found

    # --- trailers -------------------------------------------------------------------

    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None:
        """Return the best YouTube trailer, preferring the user's language, then English."""
        iso = _base_language(language)
        try:
            async with self._session() as session:
                payload = await self._get(
                    session,
                    f"/{ref.kind}/{ref.tmdb_id}/videos",
                    {"include_video_language": f"{iso},{FALLBACK_LANGUAGE},null"},
                )
        except RemoteCallError as failure:
            raise self._unreachable("trailer", failure) from None
        return _best_trailer(_rows(payload), iso)

    # --- content filters ------------------------------------------------------------

    async def excluded_genre_ids(self, filters: TitleFilters) -> frozenset[int]:
        """Resolve the administrator's excluded genre names to TMDb ids.

        Names are what an administrator can reasonably type, and TMDb's own list is what
        they have to match; a name nobody recognises is dropped rather than guessed at,
        so a typo filters nothing instead of filtering everything.
        """
        if not filters.excluded_genres:
            return frozenset()
        known = await self._genre_map()
        found: set[int] = set()
        for name in filters.excluded_genres:
            text = name.strip()
            if text.isdigit():
                found.add(int(text))
                continue
            genre_id = known.get(normalize_title(text))
            if genre_id is None:
                logger.info("a content filter names a genre TMDb does not have")
                continue
            found.add(genre_id)
        return frozenset(found)

    async def _genre_map(self) -> Mapping[str, int]:
        if self._genres is not None:
            return self._genres
        found: dict[str, int] = {}
        try:
            async with self._session() as session:
                for kind in ("movie", "tv"):
                    payload = await self._get(session, f"/genre/{kind}/list")
                    for row in _rows(payload, "genres"):
                        name, genre_id = as_text(row.get("name")), _int(row.get("id"))
                        if name is not None and genre_id is not None:
                            found.setdefault(normalize_title(name), genre_id)
        except RemoteCallError as failure:
            raise self._unreachable("genres", failure) from None
        self._genres = found
        return found


def _score(candidate: Title, wanted: str, year: int | None) -> _Score:
    titles = {normalize_title(candidate.title)}
    if candidate.original_title is not None:
        titles.add(normalize_title(candidate.original_title))
    distance = abs(candidate.year - year) if year is not None and candidate.year is not None else 0
    return _Score(
        title_matches=wanted in titles,
        year_matches=year is not None and candidate.year == year,
        year_distance=distance,
        popularity=candidate.popularity,
    )


def passes(title: Title, filters: TitleFilters, excluded_genre_ids: frozenset[int]) -> bool:
    """Whether a candidate survives the household's content filters.

    A missing year or language passes, as it does in the fork: refusing what TMDb simply
    did not fill in would quietly empty a deck.
    """
    if filters.exclude_adult and title.adult:
        return False
    if filters.min_year is not None and title.year is not None and title.year < filters.min_year:
        return False
    language = (title.original_language or "").casefold()
    if language and language in {value.casefold() for value in filters.excluded_original_languages}:
        return False
    return not (excluded_genre_ids and excluded_genre_ids.intersection(title.genre_ids))


def _base_language(language: str) -> str:
    """``fr-BE`` is French; TMDb's video and genre endpoints want the bare code."""
    return language.partition("-")[0].casefold() or FALLBACK_LANGUAGE


def _details_of(payload: Mapping[str, Any], ref: TitleRef) -> TitleDetails:
    name = as_text(payload.get("title") if ref.kind == "movie" else payload.get("name"))
    date = payload.get("release_date") if ref.kind == "movie" else payload.get("first_air_date")
    runtimes = payload.get("episode_run_time")
    episode_runtimes = cast("list[object]", runtimes) if isinstance(runtimes, list) else []
    runtime = (
        _int(payload.get("runtime"))
        if ref.kind == "movie"
        else (_int(episode_runtimes[0]) if episode_runtimes else None)
    )
    external = as_object(payload.get("external_ids")) or {}
    imdb_id = as_text(payload.get("imdb_id")) or as_text(external.get("imdb_id"))
    return TitleDetails(
        ref=ref,
        title=name or str(ref.tmdb_id),
        original_title=as_text(
            payload.get("original_title") if ref.kind == "movie" else payload.get("original_name")
        ),
        year=_year_of(date),
        overview=as_text(payload.get("overview")),
        tagline=as_text(payload.get("tagline")),
        genres=_genre_names(payload),
        runtime_minutes=runtime,
        seasons=_int(payload.get("number_of_seasons")),
        episodes=_int(payload.get("number_of_episodes")),
        poster_path=image_path(payload.get("poster_path")),
        backdrop_path=image_path(payload.get("backdrop_path")),
        original_language=as_text(payload.get("original_language")),
        vote_average=_float(payload.get("vote_average")),
        vote_count=_int(payload.get("vote_count")) or 0,
        adult=payload.get("adult") is True,
        genre_ids=_genre_ids(payload),
        imdb_id=imdb_id if imdb_id is not None and imdb_id.startswith("tt") else None,
    )


def _genre_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    found = [as_text(entry.get("name")) for entry in as_object_list(payload.get("genres"))]
    return tuple(name for name in found if name is not None)


def _provider_of(row: Mapping[str, Any], offer: OfferKind) -> Provider | None:
    provider_id, name = _int(row.get("provider_id")), as_text(row.get("provider_name"))
    if provider_id is None or name is None:
        return None
    return Provider(
        provider_id=provider_id,
        name=name,
        offer=offer,
        logo_path=image_path(row.get("logo_path")),
    )


def _providers_of(region_data: Mapping[str, Any]) -> list[Provider]:
    best: dict[int, tuple[int, int, Provider]] = {}
    for rank, (bucket, offer) in enumerate(OFFER_BUCKETS):
        for row in _rows(region_data, bucket):
            provider = _provider_of(row, offer)
            if provider is None:
                continue
            priority = _int(row.get("display_priority"))
            found = (rank, priority if priority is not None else _LAST_PRIORITY, provider)
            if provider.provider_id not in best:
                best[provider.provider_id] = found
    return [entry[2] for entry in sorted(best.values(), key=lambda entry: (entry[0], entry[1]))]


def _best_trailer(rows: Sequence[Mapping[str, Any]], iso: str) -> Trailer | None:
    ranks = {iso: 0, FALLBACK_LANGUAGE: 1}
    best: tuple[tuple[int, int, int], Trailer] | None = None
    for row in rows:
        key = as_text(row.get("key"))
        kind = as_text(row.get("type"))
        if as_text(row.get("site")) != _YOUTUBE or kind not in _TRAILER_TYPES:
            continue
        if key is None or not _YOUTUBE_KEY.fullmatch(key):
            continue
        language = as_text(row.get("iso_639_1"))
        rank = (
            _TRAILER_TYPES.index(kind),
            0 if row.get("official") is True else 1,
            ranks.get((language or "").casefold(), 2),
        )
        if best is None or rank < best[0]:
            best = (rank, Trailer(key=key, name=as_text(row.get("name")), language=language))
    return best[1] if best is not None else None
