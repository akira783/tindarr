"""Helpers for the swipe strategy and evaluation tests.

``InMemoryMetadata`` implements the ``Metadata`` port straight from a list of titles,
which is what a strategy under test needs; the harness's own tests drive the **real**
TMDb adapter over a recorded cassette instead, so both halves of the chain are covered.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.metadata import (
    Provider,
    SearchQuery,
    Title,
    TitleDetails,
    Trailer,
)
from tindarr.ports.titles import MediaKind, TitleRef


def title(  # noqa: PLR0913 - a title is a bag of fields; naming them beats a builder
    tmdb_id: int,
    name: str,
    *,
    kind: MediaKind = "movie",
    popularity: float = 1.0,
    year: int = 2016,
    language: str = "en",
    adult: bool = False,
    genre_ids: tuple[int, ...] = (878,),
) -> Title:
    """A search-result title, with only the fields a strategy reads."""
    return Title(
        ref=TitleRef(kind, tmdb_id),
        title=name,
        year=year,
        popularity=popularity,
        original_language=language,
        adult=adult,
        genre_ids=genre_ids,
    )


@dataclass
class InMemoryMetadata:
    """TMDb reduced to a dictionary, counting what was asked of it."""

    titles: Sequence[Title] = field(default_factory=tuple[Title, ...])
    calls: list[str] = field(default_factory=list[str])

    async def test(self) -> ConnectionCheck:
        self.calls.append("test")
        return ConnectionCheck(health="ok")

    async def search(self, query: SearchQuery) -> list[Title]:
        self.calls.append(f"search:{query.title}")
        wanted = query.title.casefold()
        return [row for row in self.titles if row.title.casefold() == wanted]

    async def match(self, query: SearchQuery) -> Title | None:
        return next(iter(await self.search(query)), None)

    async def details(self, ref: TitleRef, language: str) -> TitleDetails:
        self.calls.append(f"details:{ref.kind}:{ref.tmdb_id}:{language}")
        found = next((row for row in self.titles if row.ref == ref), None)
        name = found.title if found is not None else f"{ref.kind} {ref.tmdb_id}"
        return TitleDetails(ref=ref, title=name, year=found.year if found else None)

    async def watch_providers(self, ref: TitleRef, region: str) -> list[Provider]:
        self.calls.append(f"providers:{ref.tmdb_id}:{region}")
        return []

    async def trailer(self, ref: TitleRef, language: str) -> Trailer | None:
        self.calls.append(f"trailer:{ref.tmdb_id}:{language}")
        return None

    async def region_providers(self, region: str) -> list[Provider]:
        self.calls.append(f"region:{region}")
        return []
