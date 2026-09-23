"""Metadata ports: TMDb, which every card is built from, and OMDb, which enriches it.

TMDb is the authority. A suggestion is only a string until it has been matched there, so
``Metadata`` is what turns the model's free text into a ``TitleRef`` and then into the
fields a card shows. OMDb adds the three ratings TMDb does not carry (IMDb, Rotten
Tomatoes, Metacritic) and is optional: a household without an OMDb key gets cards
without those badges and nothing else changes. They are two ports rather than one for
that reason.

Everything crossing this boundary is **data**, never markup and never a URL the server
will fetch: image paths stay paths (the API prefixes them with TMDb's allow-listed base)
and a trailer is a YouTube key, never a link the adapter built
(the security model, section 6).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.titles import MediaKind, TitleRef

#: TMDb's own offer buckets, named as the HTTP contract's ``StreamingProvider.offer``.
type OfferKind = Literal["subscription", "free", "ads", "rent", "buy"]
#: The offers that mean "included in something the user already pays for or can watch
#: for nothing"; the "on your services" badge only ever lights up for these.
INCLUDED_OFFERS: frozenset[OfferKind] = frozenset({"subscription", "free", "ads"})


@dataclass(frozen=True, slots=True)
class Title:
    """A search result: enough to pick the right match, not enough to build a card."""

    ref: TitleRef
    title: str
    original_title: str | None = None
    year: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    original_language: str | None = None
    popularity: float = 0.0
    vote_average: float | None = None
    vote_count: int = 0
    adult: bool = False
    genre_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TitleDetails:
    """One title as a card shows it, in the language that was asked for."""

    ref: TitleRef
    title: str
    original_title: str | None = None
    year: int | None = None
    overview: str | None = None
    tagline: str | None = None
    genres: tuple[str, ...] = ()
    runtime_minutes: int | None = None
    seasons: int | None = None
    episodes: int | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    original_language: str | None = None
    vote_average: float | None = None
    vote_count: int = 0
    adult: bool = False
    #: TMDb genre ids, so the content filters read details and search results alike.
    genre_ids: tuple[int, ...] = ()
    #: IMDb id (``tt…``), the key OMDb is asked with. Absent for many series.
    imdb_id: str | None = None


@dataclass(frozen=True, slots=True)
class Provider:
    """One streaming offer for a title in one region."""

    provider_id: int
    name: str
    offer: OfferKind
    logo_path: str | None = None

    @property
    def included(self) -> bool:
        """Whether this offer counts for the "on your services" badge."""
        return self.offer in INCLUDED_OFFERS


@dataclass(frozen=True, slots=True)
class Trailer:
    """A YouTube trailer, as a key. The app opens it through ``youtube-nocookie.com``."""

    key: str
    name: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Ratings:
    """The ratings OMDb carries. Every field is missing far more often than not."""

    #: IMDb, on TMDb's own 0-10 scale so a card can show them side by side.
    imdb: float | None = None
    #: Rotten Tomatoes' "Tomatometer", 0-100.
    rotten_tomatoes: int | None = None
    #: Metacritic's Metascore, 0-100.
    metacritic: int | None = None

    @property
    def empty(self) -> bool:
        """Whether OMDb knew the title but had no rating for it."""
        return self.imdb is None and self.rotten_tomatoes is None and self.metacritic is None


@dataclass(frozen=True, slots=True)
class TitleFilters:
    """Titles the household never wants offered (the ``content_filters`` setting).

    The values are the administrator's own words: genre names as TMDb spells them (or
    numeric ids, for someone who prefers those) and ISO 639-1 language codes. The
    adapter resolves the names against TMDb's own genre list, so the setting does not
    have to be rewritten when TMDb adds a genre.
    """

    exclude_adult: bool = True
    min_year: int | None = None
    excluded_genres: frozenset[str] = field(default_factory=frozenset[str])
    excluded_original_languages: frozenset[str] = field(default_factory=frozenset[str])

    @property
    def empty(self) -> bool:
        """Whether nothing is filtered out at all (``exclude_adult`` aside)."""
        return (
            self.min_year is None
            and not self.excluded_genres
            and not self.excluded_original_languages
        )


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What the engine knows about a suggestion before it has been matched."""

    title: str
    kind: MediaKind
    year: int | None = None
    #: The language the model answered in; TMDb searches its translated titles too.
    language: str = "en"
    include_adult: bool = False
    #: Alternative spellings worth trying when the first search finds nothing.
    aliases: Sequence[str] = field(default_factory=tuple[str, ...])
    #: What the household refuses to be shown; applied before a match is returned.
    filters: TitleFilters | None = None


class Metadata(Protocol):
    """TMDb: search, details, watch providers and trailers."""

    async def test(self) -> ConnectionCheck:
        """Check the API key, without raising for a remote failure."""
        ...

    async def search(self, query: SearchQuery) -> list[Title]:
        """Return the candidates for a suggestion, best match first."""
        ...

    async def match(self, query: SearchQuery) -> Title | None:
        """Return the one title a suggestion means, or ``None`` when nothing fits."""
        ...

    async def details(self, ref: TitleRef, language: str) -> TitleDetails:
        """Read a title in one language, falling back to English for an empty overview."""
        ...

    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]:
        """Where a title can be streamed in one region."""
        ...

    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None:
        """Return the best YouTube trailer, falling back to English."""
        ...

    async def region_providers(self, region: str) -> list[Provider]:
        """Every streaming provider available in a region, for the user's preferences."""
        ...


class MetadataFactory(Protocol):
    """Builds the TMDb adapter for a stored key. Only ``tindarr.main`` implements it."""

    def __call__(self, api_key: str) -> Metadata:
        """Return an adapter using ``api_key``."""
        ...


class RatingsSource(Protocol):
    """OMDb: the optional enricher of the metadata port."""

    async def test(self) -> ConnectionCheck:
        """Check the API key, without raising for a remote failure."""
        ...

    async def ratings(self, imdb_id: str) -> Ratings | None:
        """Return the ratings for an IMDb id, or ``None`` when OMDb does not know it."""
        ...


class RatingsFactory(Protocol):
    """Builds the OMDb adapter for a stored key. Only ``tindarr.main`` implements it."""

    def __call__(self, api_key: str) -> RatingsSource:
        """Return an adapter using ``api_key``."""
        ...
