"""Fake TMDb and OMDb services, behind ``httpx2.MockTransport``.

The real adapters are driven against these: no network, no key, no cost. Each fake
keeps what the service would — a catalogue, the regions a title streams in, its videos —
and records every request, so a test can assert what was **not** sent (a bearer token in
a query string, a year on the retry) as easily as what was.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import parse_qs

import httpx2

#: The keys the tests configure the two connectors with.
TMDB_API_KEY: Final = "tmdb-key-0123456789abcdef"
TMDB_BEARER: Final = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0aW5kYXJyIn0.signature"
OMDB_API_KEY: Final = "omdb-key-1234"


def _json(payload: object, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=payload)


def movie_result(
    tmdb_id: int,
    title: str,
    year: int | None = 2016,
    **fields: Any,
) -> dict[str, Any]:
    """A film as TMDb serialises one in a search result."""
    row: dict[str, Any] = {
        "id": tmdb_id,
        "title": title,
        "original_title": title,
        "overview": f"{title}, in a few words.",
        "poster_path": f"/{tmdb_id}.jpg",
        "original_language": "en",
        "popularity": 10.0,
        "vote_average": 7.5,
        "vote_count": 1000,
        "adult": False,
        "genre_ids": [878],
    }
    if year is not None:
        row["release_date"] = f"{year}-11-11"
    return row | fields


def tv_result(tmdb_id: int, name: str, year: int | None = 2022, **fields: Any) -> dict[str, Any]:
    """A series as TMDb serialises one in a search result."""
    row: dict[str, Any] = {
        "id": tmdb_id,
        "name": name,
        "original_name": name,
        "overview": f"{name}, in a few words.",
        "poster_path": f"/{tmdb_id}.jpg",
        "original_language": "en",
        "popularity": 20.0,
        "vote_average": 8.2,
        "vote_count": 500,
        "genre_ids": [18],
    }
    if year is not None:
        row["first_air_date"] = f"{year}-02-18"
    return row | fields


@dataclass
class FakeTmdb:
    """TMDb, answering from memory."""

    api_key: str = TMDB_API_KEY
    bearer: str = TMDB_BEARER
    #: ``(kind, query lowercased) -> the results that search returns``.
    searches: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict[tuple[str, str], list[dict[str, Any]]]
    )
    #: ``(kind, id) -> the detail payload``, per language when a translation exists.
    details: dict[tuple[str, int, str], dict[str, Any]] = field(
        default_factory=dict[tuple[str, int, str], dict[str, Any]]
    )
    #: ``(kind, id) -> the whole ``results`` block of watch providers``.
    providers: dict[tuple[str, int], dict[str, Any]] = field(
        default_factory=dict[tuple[str, int], dict[str, Any]]
    )
    #: ``(kind, id) -> the ``results`` of the videos endpoint``.
    videos: dict[tuple[str, int], list[dict[str, Any]]] = field(
        default_factory=dict[tuple[str, int], list[dict[str, Any]]]
    )
    region_providers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    genres: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            "movie": [
                {"id": 878, "name": "Science Fiction"},
                {"id": 27, "name": "Horror"},
            ],
            "tv": [{"id": 18, "name": "Drama"}, {"id": 10767, "name": "Talk"}],
        }
    )
    #: ``path -> status`` forced on the next call to that path.
    fails: dict[str, int] = field(default_factory=dict[str, int])
    #: Set to answer a body that is not JSON at all.
    garbage: bool = False
    offline: bool = False
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    # --- helpers --------------------------------------------------------------------

    def add_search(self, kind: str, query: str, *results: dict[str, Any]) -> None:
        """Register what a search for ``query`` returns."""
        self.searches[(kind, query.casefold())] = list(results)

    def add_details(
        self, kind: str, tmdb_id: int, payload: dict[str, Any], language: str = "en"
    ) -> None:
        """Register the detail payload for one title in one language."""
        self.details[(kind, tmdb_id, language)] = payload

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the adapter talks through."""
        return httpx2.MockTransport(self.handle)

    # --- the service ----------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("api.themoviedb.org is unreachable")
        path = request.url.path.removeprefix("/3")
        forced = self.fails.pop(path, None)
        if forced is not None:
            return _json({"status_message": "forced", "status_code": 7}, forced)
        if self.garbage:
            return httpx2.Response(200, content=b"<!doctype html><p>not json")
        query = parse_qs(request.url.query.decode())
        if not self._authorised(request, query):
            return _json({"status_code": 7, "status_message": "Invalid API key"}, 401)
        if path == "/configuration":
            return _json({"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}})
        if path.startswith("/search/"):
            return self._search(path.removeprefix("/search/"), query)
        if path.startswith("/genre/") and path.endswith("/list"):
            kind = path.removeprefix("/genre/").removesuffix("/list")
            return _json({"genres": self.genres.get(kind, [])})
        if path == "/watch/providers/movie":
            return _json({"results": self.region_providers})
        return self._title(path, query)

    def _authorised(self, request: httpx2.Request, query: Mapping[str, list[str]]) -> bool:
        header = request.headers.get("authorization")
        if header is not None:
            return header == f"Bearer {self.bearer}"
        return query.get("api_key", [""])[0] == self.api_key

    def _search(self, kind: str, query: Mapping[str, list[str]]) -> httpx2.Response:
        term = query.get("query", [""])[0].casefold()
        results = list(self.searches.get((kind, term), []))
        year = query.get("year", query.get("first_air_date_year", []))
        if year:
            date_key = "release_date" if kind == "movie" else "first_air_date"
            results = [row for row in results if str(row.get(date_key, ""))[:4] == year[0]]
        return _json({"page": 1, "results": results, "total_results": len(results)})

    def _title(self, path: str, query: Mapping[str, list[str]]) -> httpx2.Response:
        parts = path.strip("/").split("/")
        min_parts = 2
        if len(parts) < min_parts or not parts[1].isdigit():
            return _json({"status_code": 34, "status_message": "not found"}, 404)
        kind, tmdb_id, tail = parts[0], int(parts[1]), "/".join(parts[min_parts:])
        if tail == "watch/providers":
            return _json({"id": tmdb_id, "results": self.providers.get((kind, tmdb_id), {})})
        if tail == "videos":
            return _json({"id": tmdb_id, "results": self.videos.get((kind, tmdb_id), [])})
        if tail:
            return _json({"status_code": 34, "status_message": "not found"}, 404)
        language = query.get("language", ["en"])[0]
        payload = self.details.get((kind, tmdb_id, language)) or self.details.get(
            (kind, tmdb_id, "en")
        )
        if payload is None:
            return _json({"status_code": 34, "status_message": "not found"}, 404)
        return _json(payload)


@dataclass
class FakeOmdb:
    """OMDb, answering from memory."""

    api_key: str = OMDB_API_KEY
    #: ``imdb id -> the payload OMDb serves``.
    titles: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    status: int = 200
    offline: bool = False
    garbage: bool = False
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the adapter talks through."""
        return httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("www.omdbapi.com is unreachable")
        if self.status != 200:
            return _json({"Response": "False", "Error": "forced"}, self.status)
        if self.garbage:
            return httpx2.Response(200, content=b"not json at all")
        query = parse_qs(request.url.query.decode())
        if query.get("apikey", [""])[0] != self.api_key:
            return _json({"Response": "False", "Error": "Invalid API key!"}, 401)
        imdb_id = query.get("i", [""])[0]
        payload = self.titles.get(imdb_id)
        if payload is None:
            return _json({"Response": "False", "Error": "Incorrect IMDb ID."})
        return _json(payload)


def omdb_title(
    imdb_rating: str = "7.9",
    metascore: str = "81",
    tomatoes: str | None = "94%",
    **fields: Any,
) -> dict[str, Any]:
    """A title as OMDb serialises one."""
    ratings: list[dict[str, str]] = [{"Source": "Internet Movie Database", "Value": "7.9/10"}]
    if tomatoes is not None:
        ratings.append({"Source": "Rotten Tomatoes", "Value": tomatoes})
    ratings.append({"Source": "Metacritic", "Value": "81/100"})
    return {
        "Title": "Arrival",
        "Response": "True",
        "imdbRating": imdb_rating,
        "imdbVotes": "1,234,567",
        "Metascore": metascore,
        "Ratings": ratings,
    } | fields


def probe_title() -> dict[str, Any]:
    """The film ``OmdbRatings.test`` asks for."""
    return omdb_title()


def raw(request: httpx2.Request) -> Mapping[str, Any]:
    """Return the JSON body of a request, for the tests that check what was sent."""
    try:
        payload: Any = json.loads(request.content or b"{}")
    except ValueError:
        return {}
    return cast("Mapping[str, Any]", payload) if isinstance(payload, dict) else {}
