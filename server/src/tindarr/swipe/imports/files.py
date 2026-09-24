"""Recognising an upload and reading it, without asking the user what it is.

Somebody who has just spent ten minutes finding the export button in three different
settings pages should not then have to tell a form which of three radio buttons their
file is. Every one of these formats announces itself in its header row, so the format is
**detected** and the user is only told what was found.

| Format | How it is recognised |
|---|---|
| Netflix, short export | exactly ``Title`` and ``Date`` |
| Netflix, long GDPR export | ``Title`` beside ``Profile Name``, ``Start Time`` or
  ``Supplemental Video Type`` |
| IMDb ratings | ``Const`` and ``Your Rating`` |
| Letterboxd | ``Letterboxd URI`` beside ``Name`` |

A ZIP is opened and its members are offered the same test, which is what makes
"Letterboxd export" mean the archive the site hands you rather than one file out of it.

**Everything is bounded before it is read**, because an upload is the one input here
that somebody else's computer wrote. The bytes are capped by the caller, and the row
count, the field length and the number of distinct titles are capped here.

An archive is bounded by a **running total across every member it reads**, not by what
it says about itself. Its central directory declares each member's inflated size, and
that number is the uploader's: ``zipfile`` does not check it until the member is read to
its end, which a reader that stops at a cap never does. So the declared sizes are used
only as a cheap early refusal, and the number that actually stops a bomb is the count of
bytes this module has inflated so far — one budget for the whole archive, so two hundred
members cannot each spend it.

Nothing is written to disk: an import is somebody's viewing history, and the only copy
of it this server keeps is the titles it managed to identify.
"""

import csv
import io
import logging
import math
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final

from tindarr.ports.titles import MediaKind
from tindarr.swipe.imports.netflix import NetflixRow, netflix_items, normalized, read_date
from tindarr.swipe.imports.records import (
    MAX_FIELD_LENGTH,
    MAX_ROWS,
    MAX_TITLES,
    MAX_UNPACKED_BYTES,
    ImportFormat,
    ParsedFile,
    UnreadableImportError,
    WatchedItem,
)

__all__ = ["LETTERBOXD_MEMBERS", "detect_and_parse"]

logger = logging.getLogger(__name__)

#: The members of a Letterboxd archive worth reading, **best last**: they are merged in
#: this order and a later one wins, so the file carrying an opinion as well as a viewing
#: is the one whose row survives. A real export ships all three, which is why reading
#: only the first would silently drop every film somebody watched and did not rate.
LETTERBOXD_MEMBERS: Final = ("watched.csv", "diary.csv", "ratings.csv")
#: A ZIP with more members than this is not somebody's film diary: Letterboxd ships
#: about eight files, and a number that looks generous is a number an attacker gets to
#: multiply the work by.
_MAX_ZIP_MEMBERS: Final = 25
#: How many members may fail to parse before the archive is refused. A real export has
#: one or two files we cannot read; a hundred is somebody looking for a parser bug.
_MAX_FAILED_MEMBERS: Final = 5
#: IMDb's ``Title Type``, mapped onto the two kinds a card can be. Anything absent from
#: this table — a video game, a podcast episode — is dropped rather than guessed at.
_IMDB_KINDS: Final[Mapping[str, MediaKind]] = {
    "movie": "movie",
    "tvmovie": "movie",
    "short": "movie",
    "tvshort": "movie",
    "video": "movie",
    "documentary": "movie",
    "tvspecial": "movie",
    "tvseries": "tv",
    "tvminiseries": "tv",
    "tvepisode": "tv",
}
#: Letterboxd stores half-star ratings out of five; TMDb's scale is out of ten.
_LETTERBOXD_RATING_SCALE: Final = 2.0
_MAX_RATING: Final = 10.0
#: The header row is looked for in the first few lines; past that it is not a CSV of ours.
_SNIFF_BYTES: Final = 64 * 1024


def detect_and_parse(payload: bytes) -> ParsedFile:
    """Recognise an upload and read it, or refuse it.

    Raises ``UnreadableImportError`` for anything that is not one of the four shapes
    above, including an archive holding none of them.
    """
    if payload[:2] == b"PK":
        return _parse_zip(payload)
    return _parse_csv_bytes(payload)


# --- the ZIP ------------------------------------------------------------------------


def _parse_zip(payload: bytes) -> ParsedFile:
    """Read a Letterboxd archive, or the first other member that parses.

    The three Letterboxd exports are **merged**, best source last so it wins: a real
    archive ships `ratings.csv` beside `watched.csv`, and reading only the first would
    silently drop every film somebody watched and did not rate.
    """
    budget = _Budget()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ZIP_MEMBERS:
                raise UnreadableImportError("this archive holds too many files")
            if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
                # The uploader wrote these numbers, so this is only an early refusal.
                raise UnreadableImportError("this archive expands to too much")
            found = _letterboxd(archive, budget)
            if found is not None:
                return found
            return _first_readable(archive, members, budget)
    except (zipfile.BadZipFile, OSError) as failure:
        raise UnreadableImportError("this file is not a readable archive") from failure


class _Budget:
    """How many inflated bytes this archive may still spend, across every member."""

    __slots__ = ("left",)

    def __init__(self, total: int = MAX_UNPACKED_BYTES) -> None:
        self.left = total

    def spend(self, size: int) -> None:
        """Take ``size`` bytes out of the budget, or refuse the archive."""
        self.left -= size
        if self.left < 0:
            raise UnreadableImportError("this archive expands to too much")


def _letterboxd(archive: zipfile.ZipFile, budget: _Budget) -> ParsedFile | None:
    """Merge the Letterboxd members an archive holds, or return ``None`` if it has none."""
    merged: dict[tuple[str, int | None], WatchedItem] = {}
    skipped: dict[str, int] = {}
    seen_one = False
    for name in LETTERBOXD_MEMBERS:
        raw = _member(archive, name, budget)
        if raw is None:
            continue
        try:
            parsed = _parse_csv_bytes(raw)
        except UnreadableImportError:
            # One unreadable member must not cost the archive: `ratings.csv` is always
            # present and is the one most likely to be empty.
            continue
        if parsed.format != "letterboxd":
            continue
        seen_one = True
        for reason, count in parsed.skipped.items():
            skipped[reason] = skipped.get(reason, 0) + count
        for item in parsed.items:
            # Later members win on a tie, and LETTERBOXD_MEMBERS is ordered so that the
            # one carrying an opinion is read last.
            merged[(normalized(item.query), item.year)] = item
    if not seen_one:
        return None
    return _capped("letterboxd", tuple(merged.values()), skipped)


def _first_readable(
    archive: zipfile.ZipFile, members: Sequence[zipfile.ZipInfo], budget: _Budget
) -> ParsedFile:
    """Read the first CSV member that turns out to be an export we know."""
    failed = 0
    for member in members:
        if member.is_dir() or not member.filename.lower().endswith(".csv"):
            continue
        try:
            return _parse_csv_bytes(_read_member(archive, member, budget))
        except UnreadableImportError:
            failed += 1
            if failed >= _MAX_FAILED_MEMBERS:
                break
    raise UnreadableImportError("this archive holds no export we recognise")


def _member(archive: zipfile.ZipFile, name: str, budget: _Budget) -> bytes | None:
    """Return one member by name, wherever it sits in the archive's directories."""
    for info in archive.infolist():
        if not info.is_dir() and info.filename.rsplit("/", 1)[-1].lower() == name:
            return _read_member(archive, info, budget)
    return None


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget) -> bytes:
    """Read one member, against the archive's remaining budget rather than its claims."""
    with archive.open(info) as handle:
        data = handle.read(budget.left + 1)
    budget.spend(len(data))
    return data


# --- the CSV ------------------------------------------------------------------------


def _parse_csv_bytes(payload: bytes) -> ParsedFile:
    """Decode an upload and hand it to the reader its header names."""
    text = _decode(payload)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        header = reader.fieldnames
    except csv.Error as failure:
        raise UnreadableImportError("this file is not a readable CSV") from failure
    if not header:
        raise UnreadableImportError("this file has no header row")
    columns = {(name or "").strip().casefold() for name in header}
    parse = _reader_for(columns)
    if parse is None:
        raise UnreadableImportError("this file is not an export we recognise")
    cut: list[int] = []
    try:
        return _with_truncation(parse(_rows(reader, cut)), cut)
    except (csv.Error, ValueError) as failure:
        # A ValueError here is a field this parser could not read at all. It is the
        # file's fault, not the server's, and the caller gets the one sentence every
        # other refusal gets rather than a 500 and a traceback.
        raise UnreadableImportError("this file is not a readable CSV") from failure


def _with_truncation(parsed: ParsedFile, cut: Sequence[int]) -> ParsedFile:
    """Record the rows the cap dropped, so a cut file never looks like a whole one."""
    if not cut:
        return parsed
    return ParsedFile(
        format=parsed.format,
        items=parsed.items,
        skipped=dict(parsed.skipped) | {"rows_over_limit": cut[0]},
    )


def _decode(payload: bytes) -> str:
    """Decode an upload as UTF-8, then as the other thing a spreadsheet writes.

    Errors are replaced rather than raised at the last resort: a single bad byte in row
    four hundred must not cost somebody their whole history, and a title with a
    replacement character in it simply fails to match and lands in the review queue,
    which is where an unreadable title belongs.
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _reader_for(
    columns: frozenset[str] | set[str],
) -> Callable[[Iterator[Mapping[str, str]]], ParsedFile] | None:
    """Return the reader whose header this is, or ``None``."""
    if "const" in columns and "your rating" in columns:
        return _read_imdb
    if "letterboxd uri" in columns and "name" in columns:
        return _read_letterboxd
    if "title" in columns and (
        "date" in columns
        or "start time" in columns
        or "profile name" in columns
        or "supplemental video type" in columns
    ):
        return _read_netflix
    return None


def _rows(reader: csv.DictReader[str], cut: list[int]) -> Iterator[Mapping[str, str]]:
    """Yield the rows, capped, with every key folded and every value cut to length.

    ``cut`` is filled with how many rows were dropped, because a file quietly cut in
    half is exactly the failure the ``skipped`` map exists to make visible.
    """
    for count, row in enumerate(reader):
        if count >= MAX_ROWS:
            cut.append(sum(1 for _ in reader) + 1)
            logger.info("an imported file was longer than the cap and was cut")
            return
        yield {
            (key or "").strip().casefold(): _field(value)
            for key, value in row.items()
            if isinstance(key, str)
        }


def _field(value: object) -> str:
    if isinstance(value, list):
        # csv gives a list for the overflow of a ragged row; it is never a field of ours.
        return ""
    return value.strip()[:MAX_FIELD_LENGTH] if isinstance(value, str) else ""


# --- one reader per format ------------------------------------------------------------


def _read_netflix(rows: Iterator[Mapping[str, str]]) -> ParsedFile:
    """Read either Netflix export: the two-column one and the long GDPR one."""
    history = [
        NetflixRow(
            title=row.get("title", ""),
            watched_at=read_date(row.get("date") or row.get("start time") or ""),
            supplemental=row.get("supplemental video type", ""),
        )
        for row in rows
    ]
    if not history:
        raise UnreadableImportError("this file holds no rows")
    items, skipped = netflix_items(history)
    return _capped("netflix", items, skipped)


def _read_imdb(rows: Iterator[Mapping[str, str]]) -> ParsedFile:
    """Read an IMDb ratings export, which carries an exact id on every row."""
    items: list[WatchedItem] = []
    skipped: dict[str, int] = {}
    for row in rows:
        const = row.get("const", "")
        kind = _IMDB_KINDS.get(row.get("title type", "").replace(" ", "").casefold())
        if not const.startswith("tt") or not const[2:].isdigit():
            skipped["no_id"] = skipped.get("no_id", 0) + 1
            continue
        if kind is None:
            skipped["not_a_title"] = skipped.get("not_a_title", 0) + 1
            continue
        items.append(
            WatchedItem(
                query=row.get("title") or row.get("original title") or const,
                kind_hint=kind,
                year=_year(row.get("year", "")),
                imdb_id=const,
                episodes=1 if row.get("title type", "").casefold() == "tvepisode" else 0,
                rating=_rating(row.get("your rating", ""), scale=1.0),
                last_watched_at=read_date(row.get("date rated", "")),
            )
        )
    if not items and not skipped:
        raise UnreadableImportError("this file holds no rows")
    return _capped("imdb", tuple(items), skipped)


def _read_letterboxd(rows: Iterator[Mapping[str, str]]) -> ParsedFile:
    """Read a Letterboxd ``watched``, ``ratings`` or ``diary`` CSV. Films only."""
    items: list[WatchedItem] = []
    skipped: dict[str, int] = {}
    for row in rows:
        name = row.get("name", "")
        if not name:
            skipped["empty"] = skipped.get("empty", 0) + 1
            continue
        items.append(
            WatchedItem(
                query=name,
                kind_hint="movie",
                year=_year(row.get("year", "")),
                rating=_rating(row.get("rating", ""), scale=_LETTERBOXD_RATING_SCALE),
                last_watched_at=read_date(row.get("watched date") or row.get("date") or ""),
            )
        )
    if not items and not skipped:
        raise UnreadableImportError("this file holds no rows")
    return _capped("letterboxd", tuple(items), skipped)


def _capped(
    kind: ImportFormat, items: Sequence[WatchedItem], skipped: Mapping[str, int]
) -> ParsedFile:
    """Cut an import to the number of titles one upload may cost, and say so."""
    counts = dict(skipped)
    kept = list(items)
    if len(kept) > MAX_TITLES:
        counts["over_limit"] = len(kept) - MAX_TITLES
        kept = kept[:MAX_TITLES]
    if not kept:
        raise UnreadableImportError("this file holds nothing we could read")
    return ParsedFile(format=kind, items=tuple(kept), skipped=counts)


def _year(value: str) -> int | None:
    """Read a year, or ``None``.

    ``isdecimal`` and not ``isdigit``: the second is true of superscripts, for which
    ``int()`` raises — and a one-line CSV that crashes a parser is a 500 somebody can
    send on purpose.
    """
    text = value.strip()[:4]
    return int(text) if text.isdecimal() else None


def _rating(value: str, *, scale: float) -> float | None:
    """Return a score on TMDb's 0-10 scale, or ``None`` when the row carried none.

    ``nan`` and ``inf`` parse as floats and survive every comparison in a clamp, so they
    are refused by name. Nothing here is currently rendered from them, and the reason to
    stop them anyway is that the only thing between ``nan`` and a permanently
    unreadable review queue is SQLite quietly storing it as ``NULL``.
    """
    try:
        score = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(score):
        return None
    clamped = min(max(score * scale, 0.0), _MAX_RATING)
    # A score of zero is "they rated it zero", not "they did not rate it".
    return None if clamped <= 0.0 else clamped
