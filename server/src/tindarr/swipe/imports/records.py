"""What a parsed import file is, before TMDb has been asked anything.

One vocabulary for three formats. A Netflix viewing history, an IMDb ratings export and
a Letterboxd archive say very different things — a list of evenings, a list of scores, a
list of films — and what they have in common is small: *this person watched this title,
this many episodes of it, that long ago, and here is what they thought of it if they
said*. That is ``WatchedItem``, and everything format-specific is gone by the time one
exists.

**Nothing here talks to TMDb, reads a clock or touches a file system.** The parsers take
bytes and give back rows, which is what lets the whole of the interesting behaviour —
the French colon, the trailers, the episode counting — be tested without a network and
without a fixture directory.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, get_args

from tindarr.ports.titles import MediaKind

__all__ = [
    "IMPORT_FORMATS",
    "MAX_FIELD_LENGTH",
    "MAX_ROWS",
    "MAX_TITLES",
    "MAX_UNPACKED_BYTES",
    "MAX_UPLOAD_BYTES",
    "ImportFormat",
    "ParsedFile",
    "UnreadableImportError",
    "WatchedItem",
]

#: The three formats a user can upload. Each is a file they already own and can export
#: themselves, which is the whole reason ADR 0013 keeps them and drops Trakt.
type ImportFormat = Literal["netflix", "imdb", "letterboxd"]

IMPORT_FORMATS: Final[tuple[ImportFormat, ...]] = get_args(ImportFormat.__value__)

#: Largest upload accepted, before anything is parsed. A decade of Netflix viewing is
#: about a megabyte of CSV and a Letterboxd archive with its diary is smaller; eight
#: megabytes is generous for every real export and small enough that a request holding
#: one in memory is not a way of emptying the server's.
MAX_UPLOAD_BYTES: Final = 8 * 1024 * 1024
#: Largest total a ZIP may expand to. A zip bomb is thirty kilobytes that becomes a
#: terabyte, so the archive's own declared sizes are checked **before** a member is read
#: and the running total is checked again while it is.
MAX_UNPACKED_BYTES: Final = 32 * 1024 * 1024
#: Most rows read from one file. Past this the file is not somebody's viewing history.
MAX_ROWS: Final = 200_000
#: Most distinct titles one import may produce, and so the most TMDb searches it can
#: cost. A file is a person's history, not a work queue somebody hands the server.
MAX_TITLES: Final = 2_000
#: Longest single field kept. A title is short; a megabyte in a CSV cell is an attack on
#: whatever renders it, and truncating is enough because the value is only ever searched
#: for and shown back to the person who uploaded it.
MAX_FIELD_LENGTH: Final = 300


class UnreadableImportError(ValueError):
    """The upload is not one of the three formats, or is too damaged to read.

    Deliberately one error for "we do not know this format", "this CSV has no header"
    and "this ZIP holds nothing we recognise". The person uploading gets told to check
    the file; they are not told what the server managed to parse out of it, which is how
    a probe learns what it can smuggle past a parser.
    """


@dataclass(frozen=True, slots=True)
class WatchedItem:
    """One title one file says this person watched, before it has been identified.

    ``query`` is the text the file carried, already reduced to the *title* — for Netflix
    that is the left-hand side of the row, with the season and the episode taken off.
    It is what TMDb is searched for and, when the search is not confident, what the
    review queue shows the user so they can recognise their own row.
    """

    query: str
    #: Which way to search first. ``None`` means the file did not say.
    kind_hint: MediaKind | None = None
    year: int | None = None
    #: An ``tt…`` id, for a source that carries one. It is resolved **exactly**, through
    #: TMDb's ``/find``, and never by searching for the title: an id that identifies a
    #: title is not a string that resembles one.
    imdb_id: str | None = None
    #: How many distinct episodes of this the file recorded. ``0`` for a film, and for a
    #: series a source counts no episodes for.
    episodes: int = 0
    #: The user's own score, on TMDb's 0-10 scale.
    rating: float | None = None
    #: The most recent time the file recorded for this title.
    last_watched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """Everything one upload turned out to hold."""

    format: ImportFormat
    items: tuple[WatchedItem, ...] = ()
    #: Rows the parser read and deliberately did not keep, by reason. The console shows
    #: this: "we dropped 12 trailers" is the difference between a parser somebody trusts
    #: and one that quietly loses half a file.
    skipped: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def rows_kept(self) -> int:
        """How many distinct titles this file produced."""
        return len(self.items)

    @property
    def rows_skipped(self) -> int:
        """How many rows were read and dropped."""
        return sum(self.skipped.values())
