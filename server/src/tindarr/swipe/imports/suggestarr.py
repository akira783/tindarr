"""``tindarr import suggestarr``: the votes somebody already cast in the fork.

A fresh Tindarr account knows nothing, and the engine of
[ADR 0013](../../../../../docs/adr/0013-recommendation-engine.md) is only as good as
the taste it is given: the first batches of an empty account are calibration, which is
three minutes of famous posters spent learning what the fork already recorded. Anybody
migrating from the SuggestArr fork this project was extracted from
([ADR 0008](../../../../../docs/adr/0008-extraction-from-suggestarr.md)) has that
answer already — the fork's ``swipe_votes`` table is the same opinions, about the same
TMDb ids, cast in the same deck.

**What this is not.** Lot 4.4's file imports (Netflix, IMDb, Letterboxd) read a *file*
somebody exported and write **history**: "they watched this", a fact with no opinion
attached, which ADR 0013 keeps as a taste signal and an exclusion rather than as a
verdict. This reads a *vote* somebody really cast in a swipe deck — a like, a dislike,
an "I have already seen this" — and writes it to the ``votes`` table, where it counts
for the taste profile, for ``liked_recall`` and for everything else a vote decides.
The two must not be confused, so they do not share a table, a code path or a word.

**The vocabulary needs no translating**, which is worth stating because it looks like
an oversight. The fork's ``vote`` column holds ``like``, ``dislike``, ``seen_liked``
and ``seen_disliked``; Tindarr's ``VoteValue`` is those four plus ``skip``. Its
``pick_type`` holds ``safe``, ``explore`` and ``calibration``; Tindarr's ``PickKind``
is exactly those three. Both were extracted from the fork, so a mapping table here
would be an identity function pretending to be a decision. A value outside the two
vocabularies is dropped and counted, never guessed at.

**The fork's database is opened read-only and is never written to.** It is somebody's
running instance: ``mode=ro`` on the connection URI, no migration, no schema check that
writes, and the importer's own bookkeeping lives in *Tindarr's* database. The operator
is still told, by ``docs/roadmap.md`` and by the command's own help, to point it at a
copy — a read-only open is a promise about this process, not about the disk.

**Running it twice imports nothing twice.** Every row gets a deterministic receipt —
the fork's own primary key ``(user_id, tmdb_id, media_type)``, namespaced — written
through ``vote_receipts``, the table the offline swipe queue already uses to answer
"have I stored this one before?". The insert *is* the test, so a second run, an
interrupted first run and two runs racing each other all end with one vote per row.
A receipt outlives the vote it produced, which is the point: somebody who imported
their fork history, then changed their mind about a film in Tindarr, does not get the
fork's older opinion put back on top of the newer one by a second import.

**Which Tindarr account.** The fork keys its votes by its own ``auth_users`` id, which
means nothing here. What both sides can agree on is the **media server** account:
Tindarr stores it as ``users.media_server_user_id`` and the fork, when its owner has
linked one, as ``user_media_profiles.external_user_id``. So the mapping is that column
against that column, and nothing else — never the username, which is a display name on
both sides and is exactly the kind of match that silently gives one household's votes
to another person. When the fork has no linked profile, or when more than one could
answer, the import **refuses and says so**, and ``--user`` names the target account
explicitly. Refusing is the whole value of the check: a wrong answer here is somebody
else's taste, permanently, in an account that will never mention where it came from.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.ports.deck import PickKind, VoteValue, as_pick_kind, as_vote_value
from tindarr.ports.titles import TitleRef, as_media_kind
from tindarr.storage import users as user_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.votes import IMPORT_RECEIPT_PREFIX

__all__ = [
    "RECEIPT_NAMESPACE",
    "ForkDatabaseError",
    "ForkIdentity",
    "ForkVote",
    "ImportReport",
    "MappingError",
    "read_identities",
    "read_votes",
    "receipt_id",
    "resolve_target",
    "store_votes",
]

logger = logging.getLogger(__name__)

#: What a receipt from this importer is prefixed with, so it can never collide with a
#: ``client_vote_id`` a phone chose. A phone's ids are opaque strings it generates; this
#: one is a sentence about a row in somebody else's database, and the two live in the
#: same column.
RECEIPT_NAMESPACE: Final = f"{IMPORT_RECEIPT_PREFIX}suggestarr"

#: The fork's own table. Its absence is what tells "this is not a SuggestArr database"
#: apart from "this database has no votes in it yet".
VOTES_TABLE: Final = "swipe_votes"
#: Where the fork records the media server account behind one of its users. Optional:
#: the fork works without it, and an instance that never linked one simply has none.
PROFILES_TABLE: Final = "user_media_profiles"

#: Ordered so that a re-run reads the rows in the same order it did the first time, and
#: so that "the order somebody voted" survives into ``voted_at``. ``updated_at`` breaks
#: the tie for rows written in the same second, and the TMDb id breaks it after that,
#: because two rows with identical timestamps must still not swap places between runs.
_VOTES_QUERY: Final = (
    "SELECT user_id, tmdb_id, media_type, vote, pick_type, requested, title, year, created_at "
    "FROM swipe_votes ORDER BY created_at, updated_at, tmdb_id"
)

_PROFILES_QUERY: Final = (
    "SELECT user_id, provider, external_user_id, external_username "
    "FROM user_media_profiles ORDER BY user_id, provider"
)

#: Longest fork-supplied string kept on a vote. A title is short; a megabyte in a text
#: column is an attack on whatever renders it back, and the same bound is what lot 4.4's
#: file parser applies to a CSV cell (``tindarr.swipe.imports.records``).
MAX_FIELD_LENGTH: Final = 300


class ForkDatabaseError(ValueError):
    """The file is not a readable SuggestArr database.

    One error for "no such file", "not a database", "no ``swipe_votes`` table" and "the
    table has not got the columns this reads", because the operator's next move is the
    same in all four: check what they pointed the command at. The underlying SQLite
    message is carried in the detail, since unlike an upload this file is the operator's
    own and telling them what went wrong costs nobody anything.
    """


class MappingError(ValueError):
    """The fork's users could not be matched to exactly one Tindarr account.

    Raised rather than resolved by a guess. ``--user`` is the answer, and the message
    says which accounts were on offer.
    """


@dataclass(frozen=True, slots=True)
class ForkVote:
    """One row of the fork's ``swipe_votes``, once it has been understood.

    ``fork_user_id`` is the fork's own ``auth_users`` id. It is kept so the rows can be
    grouped per person and so the receipt can name the row it came from; it is never
    stored on the vote, because it identifies nothing outside the fork.
    """

    fork_user_id: int
    ref: TitleRef
    value: VoteValue
    pick: PickKind
    requested: bool
    title: str
    year: int | None
    voted_at: datetime | None


@dataclass(frozen=True, slots=True)
class ForkIdentity:
    """A media server account the fork has linked to one of its users."""

    fork_user_id: int
    provider: str
    external_user_id: str
    external_username: str | None


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What one import did, in the four numbers that can differ between two runs."""

    #: Votes written for the first time.
    imported: int = 0
    #: Rows whose receipt was already there: a previous run stored them.
    already_imported: int = 0
    #: Rows a newer Tindarr vote about the same title beat (``record`` returned False).
    superseded: int = 0
    #: Rows that could not be read as a vote at all: an unknown verdict, a bad id.
    unreadable: int = 0

    @property
    def total(self) -> int:
        """Every row this import looked at."""
        return self.imported + self.already_imported + self.superseded + self.unreadable


def receipt_id(vote: ForkVote) -> str:
    """Return the deterministic receipt for one fork row.

    Built from the fork's own primary key — ``(user_id, tmdb_id, media_type)`` — so it
    is stable across runs, across re-orderings and across a fork that has since been
    voted in again. It deliberately does **not** include the verdict: a row whose vote
    changed in the fork after an import is *not* a new row, and re-importing it would
    silently overwrite whatever the person has since said in Tindarr.
    """
    return f"{RECEIPT_NAMESPACE}:{vote.fork_user_id}:{vote.ref.kind}:{vote.ref.tmdb_id}"


def read_votes(database: Path) -> tuple[tuple[ForkVote, ...], int]:
    """Read every vote in the fork's database, and how many rows made no sense.

    Opened read-only. The rows are returned in the fork's own chronological order, which
    is what ``voted_at`` is taken from and what the newest-swipe-wins guard in
    ``tindarr.storage.votes.record`` compares against.
    """
    connection = _open(database)
    try:
        if VOTES_TABLE not in _tables(connection):
            msg = (
                f"{database} has no '{VOTES_TABLE}' table, so it is not a SuggestArr "
                "database this command can read"
            )
            raise ForkDatabaseError(msg)
        rows = _query(connection, _VOTES_QUERY, database)
    finally:
        connection.close()
    understood: list[ForkVote] = []
    unreadable = 0
    for row in rows:
        vote = _vote_of(row)
        if vote is None:
            unreadable += 1
            continue
        understood.append(vote)
    return tuple(understood), unreadable


def read_identities(database: Path) -> tuple[ForkIdentity, ...]:
    """Read the media server accounts the fork has linked, or nothing when it has none.

    A fork that never linked one has an empty table, and older forks have no table at
    all. Neither is an error: both mean "this database cannot say which account its
    users are", which is what ``resolve_target`` turns into a request for ``--user``.
    """
    connection = _open(database)
    try:
        if PROFILES_TABLE not in _tables(connection):
            return ()
        rows = _query(connection, _PROFILES_QUERY, database)
    finally:
        connection.close()
    found: list[ForkIdentity] = []
    for row in rows:
        fork_user_id = _whole(row[0])
        provider = _text(row[1])
        external = _text(row[2])
        if fork_user_id is None or provider is None or external is None:
            continue
        found.append(
            ForkIdentity(
                fork_user_id=fork_user_id,
                provider=provider,
                external_user_id=external,
                external_username=_text(row[3]),
            )
        )
    return tuple(found)


async def resolve_target(
    connection: AsyncConnection,
    identities: Sequence[ForkIdentity],
    fork_user_id: int,
    *,
    override: str | None,
) -> str:
    """Return the Tindarr user id one fork user's votes belong to.

    ``override`` is ``--user``: a Tindarr user id, taken as given after checking it
    exists, because an operator who names an account has answered the question this
    function exists to ask.

    Without it, the fork's linked media server account is matched against
    ``users.media_server_user_id``. Anything other than exactly one answer raises:
    no linked profile, a profile pointing at an account Tindarr has never seen, or two
    profiles for the same fork user that resolve to different Tindarr accounts.
    """
    if override is not None:
        user = await user_repository.get(connection, override)
        if user is None:
            msg = f"no Tindarr user has the id '{override}'.{await _on_offer(connection)}"
            raise MappingError(msg)
        return user.id
    linked = [identity for identity in identities if identity.fork_user_id == fork_user_id]
    if not linked:
        msg = (
            f"the fork's user {fork_user_id} has no linked media server account, so there "
            f"is nothing to match a Tindarr account on. Name one with "
            f"--user <user-id>.{await _on_offer(connection)}"
        )
        raise MappingError(msg)
    matched: set[str] = set()
    for identity in linked:
        user = await user_repository.get_by_media_server_id(connection, identity.external_user_id)
        if user is not None:
            matched.add(user.id)
    if len(matched) == 1:
        return next(iter(matched))
    tried = ", ".join(sorted(identity.external_user_id for identity in linked))
    msg = (
        f"the fork's user {fork_user_id} maps to {len(matched)} Tindarr accounts "
        f"(media server ids tried: {tried}). Name one with "
        f"--user <user-id>.{await _on_offer(connection)}"
    )
    raise MappingError(msg)


async def _on_offer(connection: AsyncConnection) -> str:
    """Return the accounts ``--user`` could name, for the end of a refusal message.

    A refusal that says "name an account" and then makes the operator go and find the id
    in a database is a refusal that will be answered with a guess. There is no console
    page and no other command that lists these, so the error itself carries them.
    """
    found = await user_repository.list_all(connection)
    if not found:
        return " This instance has no users yet: set it up and sign in once first."
    listed = ", ".join(f"{user.id} ({user.name})" for user in found)
    return f" The accounts on this instance are: {listed}."


async def store_votes(
    connection: AsyncConnection,
    user_id: str,
    fork_votes: Sequence[ForkVote],
    *,
    now: datetime,
    unreadable: int = 0,
) -> ImportReport:
    """Write the fork's votes for one Tindarr user, once each, and report what happened.

    Each row is claimed with its receipt first. A receipt that was already there means a
    previous run stored it, so the vote is not written again — not even to refresh it,
    because the person may have changed their mind in Tindarr since and the fork's old
    answer must not win that argument.

    ``voted_at`` carries the fork's own timestamp, so ``record``'s newest-swipe-wins
    guard does the right thing on its own: a fork vote from last week loses to a Tindarr
    vote from yesterday about the same title, and the report counts it as superseded.
    A row the fork never dated is treated as being as old as the import is new, which is
    the conservative reading — it cannot beat anything already in the account.
    """
    imported = already = superseded = 0
    for fork_vote in fork_votes:
        claimed = await vote_repository.remember_receipt(
            connection, user_id, receipt_id(fork_vote), now=now
        )
        if not claimed:
            already += 1
            continue
        stored = await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=fork_vote.ref,
                value=fork_vote.value,
                # No card: this opinion was not produced by a card this server served,
                # and inventing one would make the provenance column a lie.
                card_id=None,
                pick_type=fork_vote.pick,
                title=fork_vote.title,
                year=fork_vote.year,
                poster_path=None,
            ),
            voted_at=fork_vote.voted_at or now,
            now=now,
        )
        if stored:
            imported += 1
        else:
            superseded += 1
    logger.info(
        "suggestarr import finished",
        extra={
            "user_id": user_id,
            "imported": imported,
            "already_imported": already,
            "superseded": superseded,
            "unreadable": unreadable,
        },
    )
    return ImportReport(
        imported=imported,
        already_imported=already,
        superseded=superseded,
        unreadable=unreadable,
    )


def _open(database: Path) -> sqlite3.Connection:
    """Open the fork's database read-only, or say why it could not be.

    ``mode=ro`` refuses to create the file and refuses to write to it, so pointing this
    at a running instance cannot damage it. The path goes through ``as_uri`` because a
    ``?`` or a ``#`` in a directory name would otherwise be read as part of the URI.
    """
    if not database.is_file():
        msg = f"no database at {database}"
        raise ForkDatabaseError(msg)
    try:
        return sqlite3.connect(
            f"file:{database.resolve().as_uri().removeprefix('file:')}?mode=ro", uri=True
        )
    except sqlite3.Error as failure:
        msg = f"{database} could not be opened read-only: {failure}"
        raise ForkDatabaseError(msg) from failure


def _tables(connection: sqlite3.Connection) -> frozenset[str]:
    try:
        found = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return frozenset(str(row[0]) for row in found)
    except sqlite3.Error as failure:
        msg = f"the file is not a readable SQLite database: {failure}"
        raise ForkDatabaseError(msg) from failure


def _query(connection: sqlite3.Connection, query: str, database: Path) -> list[tuple[object, ...]]:
    try:
        return [tuple(row) for row in connection.execute(query)]
    except sqlite3.Error as failure:
        msg = f"{database} has a table this command cannot read: {failure}"
        raise ForkDatabaseError(msg) from failure


def _vote_of(row: Sequence[object]) -> ForkVote | None:
    """Turn one fork row into a vote, or ``None`` when it is not one.

    Everything is checked rather than trusted. The fork stores ``tmdb_id`` as text and
    nothing stops a row holding an empty string; ``vote`` and ``pick_type`` are free
    text columns with no constraint behind them. A row that fails any of it is counted
    and dropped, because a vote nobody can read is not a vote to guess at.
    """
    fork_user_id = _whole(row[0])
    tmdb_id = _whole(row[1])
    kind = as_media_kind(_text(row[2]))
    value = as_vote_value(_text(row[3]))
    pick = as_pick_kind(_text(row[4])) or "safe"
    title = _text(row[6])
    if fork_user_id is None or tmdb_id is None or tmdb_id <= 0 or kind is None or value is None:
        return None
    return ForkVote(
        fork_user_id=fork_user_id,
        ref=TitleRef(kind=kind, tmdb_id=tmdb_id),
        value=value,
        pick=pick,
        requested=bool(_whole(row[5])),
        # A vote row must carry a title, because the card it came from is long gone and
        # the votes table shows this string back to the person. The id is the honest
        # fallback: it says "the fork did not tell us" without inventing a name.
        title=title or f"TMDb {tmdb_id}",
        year=_whole(row[7]),
        voted_at=_moment(row[8]),
    )


def _whole(value: object) -> int | None:
    """Return ``value`` as an integer, or ``None``. Text is accepted: the fork uses it."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _text(value: object) -> str | None:
    """Return ``value`` as a bounded, trimmed string, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()[:MAX_FIELD_LENGTH]
    return trimmed or None


def _moment(value: object) -> datetime | None:
    """Read one of the fork's timestamps, or ``None`` when it is unreadable.

    SQLite has no date type, and the fork writes ``CURRENT_TIMESTAMP``, which is UTC
    without a zone marker. So a naive value read here is UTC and is labelled as such:
    a naive datetime reaching the votes table would be compared against aware ones.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
