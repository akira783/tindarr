"""What somebody has already watched, learned anywhere but the deck.

[ADR 0013](../../../docs/adr/0013-recommendation-engine.md) measured the obvious
idea and found it small: on the author's 99 votes, importing Netflix, Jellyfin and Seerr
between them would have avoided **3 of the 47** already-seen cards. So nothing here is
sold as the fix for the already-seen problem — the popularity band and the calibration
grid are. What an import is genuinely good at is the other half of the ADR's point:
*engagement*. Counting the episodes somebody watched of a series, against the total TMDb
knows, reproduces the "finished / in progress / sampled and dropped" signal the engine
already derives from a media server, over everything watched outside it. Thirty series
the author sampled and dropped are thirty high-rated series a recommender would happily
have proposed, and no other source says so.

It is a **port-level vocabulary** rather than a swipe-engine type, for the same reason
``Engagement`` is: the storage layer writes these rows and the swipe layer reads them,
and a type that lived above the storage layer could not be either.

Two things therefore live here, and they are deliberately the same thing:

- an **import** writes one row per title a file says the person watched;
- a **grid tick** writes one row per famous poster they said they knew.

Both are ``WatchedTitle``. Neither is a **vote**. A vote is an opinion about a card this
deck served, it is what the stats count and what the strategies replay, and a title
somebody ticked on a wall of posters is none of that. The two vocabularies are kept
apart on purpose: ``tindarr.swipe.votes`` is what the user said *to the deck*, and this
is what the world already knew. The engine reads both; only one of them is a swipe.

A ``WatchedTitle`` with ``seen`` false is not a mistake either. The grid asks a question,
and "no, never seen it" is an answer worth storing: it keeps the title off the next grid
without keeping it out of the deck, where it is now a rather good candidate.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args

from tindarr.ports.media_server import Engagement, EngagementState, LibraryItem
from tindarr.ports.titles import TitleRef

__all__ = [
    "HISTORY_SOURCES",
    "IMPORT_SOURCES",
    "HistorySource",
    "WatchedTitle",
    "as_history_source",
]

#: Where a row came from. The three file formats, plus the calibration grid.
type HistorySource = Literal["netflix", "imdb", "letterboxd", "grid"]

HISTORY_SOURCES: Final[tuple[HistorySource, ...]] = get_args(HistorySource.__value__)
#: The sources a file import can write. ``grid`` is not one: nobody uploads a grid.
IMPORT_SOURCES: Final[tuple[HistorySource, ...]] = ("netflix", "imdb", "letterboxd")


def as_history_source(value: object) -> HistorySource | None:
    """Return ``value`` as a history source, or ``None`` when it is not one.

    Rows are read back from a database this program wrote, but a database is a file an
    operator can edit, so the value is narrowed rather than trusted.
    """
    return next((source for source in HISTORY_SOURCES if source == value), None)


@dataclass(frozen=True, slots=True)
class WatchedTitle:
    """One title one source says this person has watched, and how much of it.

    ``state`` is deliberately the media server port's own ``EngagementState``, computed
    with the same two functions and the same thresholds
    (``tindarr.ports.media_server.series_engagement`` and ``film_engagement``). ADR 0013
    asks for the imports to *reproduce* the media server's engagement signal over
    everything watched elsewhere, and two definitions of "gave up on it" — one for
    Jellyfin, one for a Netflix file — would be two decks disagreeing about the same
    evening.
    """

    ref: TitleRef
    source: HistorySource
    #: Whether this is "I watched it". ``False`` is a grid poster the user did not know.
    seen: bool = True
    #: How the engine reads it, or ``None`` when the source says nothing about extent
    #: (a grid tick is "I have seen this", not "I finished it").
    state: EngagementState | None = None
    #: Fraction watched, of the episodes for a series. ``0.0`` when unknown.
    progress: float = 0.0
    episodes_played: int | None = None
    episodes_total: int | None = None
    #: The user's own score on TMDb's 0-10 scale, when the source carried one. IMDb and
    #: Letterboxd do; a viewing history does not.
    rating: float | None = None
    last_watched_at: datetime | None = None

    def as_engagement(self) -> Engagement | None:
        """Return this row as the engine's ``Engagement``, or ``None`` when it is not one.

        The item id names the source (``netflix:1399``) rather than a media server's
        item, because there is no item: nothing here is in the library, which is exactly
        why an imported title has to be excluded from the pool by hand rather than by
        ``LibraryIndex``. A row with no ``state`` — a grid tick — is not an engagement
        and says so, instead of being flattened into a "watched" the user never claimed.
        """
        if not self.seen or self.state is None:
            return None
        return Engagement(
            item=LibraryItem(
                kind=self.ref.kind,
                item_id=f"{self.source}:{self.ref.tmdb_id}",
                name="",
                tmdb_id=self.ref.tmdb_id,
            ),
            state=self.state,
            progress=self.progress,
            episodes_played=self.episodes_played,
            episodes_total=self.episodes_total,
            last_played_at=self.last_watched_at,
        )
