"""Reading a Netflix viewing history, which is one column of ambiguous prose.

A Netflix export says almost nothing: a title and a date, one row per thing played.
There is no id, no media type, no season number as a number, and the title is several
facts joined with a colon. Every comparable project resolves that by searching for the
whole string and taking ``results[0]``, which is why every comparable project has a
library full of wrong films.

Four rules do most of the work, and all four were validated against a real five-month
export before any of this was written.

**Split on ``": "``, never on ``" : "``.** Netflix joins the parts of a row with a colon
and one space after it. French distributors put a space *before* the colon inside the
title itself — *Avatar : Le dernier maître de l'air*, *The Handmaid's Tale : La Servante
écarlate*, *Suits : avocats sur mesure*. A naive ``split(": ")`` cuts those three in
half and then searches TMDb for "Avatar", which exists and is a different thing
entirely. The lookbehind in ``_SEPARATOR`` is the whole fix.

**A season marker decides the media type.** ``Saison 3``, ``Season 3``, ``Partie 2``,
``Mini-série``, ``1st Saison``, ``Limited Series``: when the second part is one of
those, the row is an episode of a series and the first part is the series. When it is
not, a row with three parts is still a series with a *named* season (*Overlord:
Overlord III: PVP*), and a row with two parts is genuinely ambiguous — *Away: Le point
de non-retour* is an episode of a series and *Shazam! La Rage des dieux* is a film.

**Ambiguity is resolved by counting, not by guessing.** A title that appears once with
one trailing part stays ambiguous and is searched as a film first. A title that appears
with *two different* trailing parts is a series: nothing else has two of them. That is
the one rule here the prototype did not have, and it is what turns *Away* from a film
nobody watched into a series somebody watched two episodes of.

**Trailers are not viewings.** The long GDPR export marks them in ``Supplemental Video
Type`` and they are dropped on that column alone. The short export — the one the
"viewing activity" page downloads — has no such column, so they are recognised by the
words distributors use for them, applied to the *trailing* part only: "un large aperçu"
is a trailer, and a film called *Le Teaser* would keep its row.
"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from tindarr.swipe.imports.records import MAX_FIELD_LENGTH, WatchedItem

__all__ = [
    "AMBIGUOUS_SERIES_PARTS",
    "NetflixRow",
    "is_supplemental",
    "netflix_items",
    "normalized",
    "parse_title",
    "read_date",
]

#: Netflix's own join: a colon and a space, with **no** space before the colon. The
#: lookbehind is what keeps a French title whole, and it excludes **any** whitespace and
#: not only U+0020: French typography puts a no-break space before a colon, so half the
#: real rows spell the very thing this must not split with a ``\xa0``. The prototype
#: this comes from used a plain space and cut *The Handmaid's Tale : La Servante
#: écarlate* in two on exactly those rows.
_SEPARATOR: Final = re.compile(r"(?<!\s): ")
#: The second part of a row, when it names a season rather than an episode.
_SEASON: Final = re.compile(
    r"^(?:Saison|Season|Partie|Part|Volume|Vol\.?|Chapitre|Chapter|Temporada|Staffel)\s*\d+"
    r"|^\d+(?:st|nd|rd|th|re|e|ère|ème)\s+(?:Saison|Season)"
    r"|^(?:Mini-série|Mini-serie|Limited Series|Série limitée|Miniseries)$",
    re.IGNORECASE,
)
#: A trailing part that numbers an episode. It is the one episode marker as reliable as
#: a season marker — *GIGN: Épisode 1* is a documentary series and nothing else — and it
#: settles a two-part row that the counting rule would otherwise have to leave ambiguous.
_EPISODE: Final = re.compile(
    r"^(?:[ÉE]pisode|Episode|Chapitre|Chapter|Folge|Cap[íi]tulo)\s*\d+\b",
    re.IGNORECASE,
)
#: What distributors call something that is not the programme. Matched against the
#: trailing part of a row only, so a film whose own title is one of these survives.
_SUPPLEMENTAL: Final = re.compile(
    r"aper[çc]u|bande[- ]annonce|trailer|teaser|coulisses|making[- ]of|"
    r"behind the scenes|sneak peek|recap|previously on|r[ée]capitulatif",
    re.IGNORECASE,
)
#: Two different trailing parts under one title mean a series. One means nothing.
AMBIGUOUS_SERIES_PARTS: Final = 2
_TITLE_PARTS: Final = 2
#: Netflix writes ``M/D/YY`` in the short export and ISO-ish stamps in the long one.
_DATE_FORMATS: Final = ("%m/%d/%y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d")
_ISO_LENGTH: Final = 10


def normalized(value: str) -> str:
    """Return a title reduced to what two spellings of it have in common.

    Case, accents and punctuation all differ between what a distributor writes into a
    viewing history and what TMDb stores, and none of them distinguishes two real
    titles. ``&`` becomes ``et`` before anything else: French catalogues write *Ducks &
    Co* and *Ducks et Co* interchangeably, and dropping the ampersand as punctuation
    would leave two strings that no longer resemble each other at all.

    Deliberately **not** ``tindarr.adapters.tmdb.normalize_title``, which is an adapter's
    and does not do the ampersand; the two answer different questions and a shared
    helper that did both would be the wrong one for each.
    """
    text = unicodedata.normalize("NFKD", value.casefold().replace("&", " et "))
    text = "".join(character for character in text if not unicodedata.combining(character))
    kept = "".join(character if character.isalnum() else " " for character in text)
    return " ".join(kept.split())


@dataclass(frozen=True, slots=True)
class NetflixRow:
    """One row of a viewing history, as the two export shapes agree on it."""

    title: str
    watched_at: datetime | None = None
    #: The long export's ``Supplemental Video Type``. Non-empty means "not the
    #: programme": a trailer, a hook, a recap.
    supplemental: str = ""


@dataclass(frozen=True, slots=True)
class _Parsed:
    """What one row's title turned out to be."""

    show: str
    #: The episode or season text, when the row had one. Two distinct ones under the
    #: same show are what makes it a series.
    part: str | None
    #: ``True`` when a season marker settled it, ``False`` when only counting can.
    certain: bool


def parse_title(title: str) -> tuple[str, str | None, bool]:
    """Split one Netflix title into ``(show, episode-ish part, is certainly a series)``.

    The rules are the module docstring's. Returns the whole string as the show when the
    row has no Netflix separator in it at all, which is what a film looks like.
    """
    parsed = _parse(title)
    return parsed.show, parsed.part, parsed.certain


def _parse(title: str) -> _Parsed:
    parts = _SEPARATOR.split(title.strip())
    if len(parts) == 1:
        return _Parsed(parts[0], None, certain=False)
    show, rest = parts[0], parts[1:]
    if _SEASON.match(rest[0]):
        # "Show: Saison 1: Episode name" — the season marker settles it.
        return _Parsed(show, ": ".join(rest) or None, certain=True)
    if len(rest) >= _TITLE_PARTS:
        # "Overlord: Overlord III: PVP" — a named season, still three parts deep.
        return _Parsed(show, ": ".join(rest), certain=True)
    return _Parsed(show, rest[0], certain=_EPISODE.match(rest[0]) is not None)


def is_supplemental(row: NetflixRow) -> bool:
    """Whether a row is a trailer, a hook or a recap rather than a viewing.

    The long export's own column first, because it is a fact rather than a guess; the
    words only when there is no column, and only against the trailing part.
    """
    if row.supplemental.strip():
        return True
    _, part, _ = parse_title(row.title)
    return part is not None and _SUPPLEMENTAL.search(part) is not None


def read_date(value: str) -> datetime | None:
    """Read one of the date shapes Netflix writes, or return ``None``.

    A row whose date cannot be read is kept: the title is the useful part, and the date
    only ever decides between "in progress" and "gave up on it".
    """
    text = value.strip()
    if not text:
        return None
    for shape in _DATE_FORMATS:
        try:
            return datetime.strptime(text, shape).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:_ISO_LENGTH]).replace(tzinfo=UTC)
    except ValueError:
        return None


def netflix_items(rows: Iterable[NetflixRow]) -> tuple[tuple[WatchedItem, ...], Mapping[str, int]]:
    """Group a viewing history into one item per title, with its episodes counted.

    Returns the items and a count of what was dropped, by reason, so the console can say
    "12 trailers" rather than silently losing them.
    """
    skipped: dict[str, int] = defaultdict(int)
    parts: dict[str, set[str]] = defaultdict(set)
    certain: set[str] = set()
    last_seen: dict[str, datetime] = {}
    order: list[str] = []
    known: set[str] = set()

    for row in rows:
        title = row.title.strip()[:MAX_FIELD_LENGTH]
        if not title:
            skipped["empty"] += 1
            continue
        if is_supplemental(row):
            skipped["supplemental"] += 1
            continue
        parsed = _parse(title)
        show, part, is_series = parsed.show.strip(), parsed.part, parsed.certain
        if not show:
            skipped["empty"] += 1
            continue
        if show not in known:
            known.add(show)
            order.append(show)
        if part is not None:
            parts[show].add(part)
        if is_series:
            certain.add(show)
        when = row.watched_at
        if when is not None and (show not in last_seen or when > last_seen[show]):
            last_seen[show] = when

    items = tuple(
        _item(show, parts[show], last_seen.get(show), series=show in certain) for show in order
    )
    return items, dict(skipped)


def _item(show: str, episodes: set[str], last: datetime | None, *, series: bool) -> WatchedItem:
    """Turn one grouped show into an item, deciding what kind of thing it is."""
    # Certain from a season marker, or certain from arithmetic: one title cannot have
    # two different trailing parts and be a film.
    is_series = series or len(episodes) >= AMBIGUOUS_SERIES_PARTS
    return WatchedItem(
        query=show,
        kind_hint="tv" if is_series else None,
        episodes=len(episodes) if is_series else 0,
        last_watched_at=last,
    )
