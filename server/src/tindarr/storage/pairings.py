"""The ``pairings`` table: rows and the state changes they go through.

A pairing walks one path and never goes back: ``pending`` (the console made a code),
``awaiting_approval`` (a phone asked, with its PKCE challenge), ``approved`` (the user
said yes in the console), ``completed`` (the phone collected its tokens). ``revoked``
is where cancelling and rejecting both end, and expiry is not a state at all: it is
``expires_at`` compared with now, so a pairing nobody touched needs no sweep to stop
working.

Every change is a **compare-and-set**: the row only moves when it is still in the state
the caller read, and still unexpired. Two phones racing on one code, or a phone
completing exactly as the user rejects, therefore cannot both win. Callers run them
inside ``write_transaction`` so the read that decides and the write that acts cannot
interleave.

As everywhere in ``tindarr.storage``, nothing here knows what a code is worth: the
service (``tindarr.auth.pairing``) hashes it, times it and decides what each refusal
answers.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import Row, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.storage.ids import new_id
from tindarr.storage.sessions import Device
from tindarr.storage.tables import pairings

type PairingStatus = Literal["pending", "awaiting_approval", "approved", "completed", "revoked"]
#: What the contract shows, which adds the state computed from ``expires_at``.
type PairingState = Literal[
    "pending", "awaiting_approval", "approved", "completed", "expired", "revoked"
]

#: States a pairing can still leave: they are the ones expiry applies to.
OPEN_STATUSES: Final[tuple[PairingStatus, ...]] = ("pending", "awaiting_approval", "approved")


@dataclass(frozen=True, slots=True)
class Pairing:
    """One row of ``pairings``."""

    id: str
    user_id: str
    code_hash: str
    status: PairingStatus
    code_challenge: str | None
    device_name: str | None
    platform: str | None
    app_version: str | None
    requested_from: str | None
    created_at: datetime
    expires_at: datetime
    requested_at: datetime | None
    approved_at: datetime | None
    completed_at: datetime | None
    revoked_at: datetime | None
    session_id: str | None

    def expired(self, now: datetime) -> bool:
        """Whether an open pairing ran out of time."""
        return self.status in OPEN_STATUSES and now >= self.expires_at

    def state(self, now: datetime) -> PairingState:
        """Return the state the contract shows: the stored status, or ``expired``."""
        return "expired" if self.expired(now) else self.status

    @property
    def device(self) -> Device:
        """The phone that asked to pair, as the session it opens will record it."""
        return Device(self.device_name, self.platform, self.app_version)

    @property
    def was_requested(self) -> bool:
        """Whether a phone ever asked on this pairing (so a rejection is one)."""
        return self.requested_at is not None


def _to_pairing(row: Row[tuple[Any, ...]]) -> Pairing:
    # Positional: ``Pairing`` lists the columns of ``pairings`` in order (a test checks it).
    return Pairing(*row)


def new_pairing(user_id: str, code_hash: str, *, now: datetime, expires_at: datetime) -> Pairing:
    """Build a ``pending`` pairing row (not stored yet)."""
    return Pairing(
        id=new_id(),
        user_id=user_id,
        code_hash=code_hash,
        status="pending",
        code_challenge=None,
        device_name=None,
        platform=None,
        app_version=None,
        requested_from=None,
        created_at=now,
        expires_at=expires_at,
        requested_at=None,
        approved_at=None,
        completed_at=None,
        revoked_at=None,
        session_id=None,
    )


async def insert(connection: AsyncConnection, pairing: Pairing) -> Pairing:
    """Store a new pairing row and return it."""
    await connection.execute(pairings.insert().values(**asdict(pairing)))
    return pairing


async def get(connection: AsyncConnection, pairing_id: str) -> Pairing | None:
    """Return the pairing with this id."""
    row = (
        await connection.execute(pairings.select().where(pairings.c.id == pairing_id))
    ).one_or_none()
    return None if row is None else _to_pairing(row)


async def get_by_code_hash(connection: AsyncConnection, code_hash: str) -> Pairing | None:
    """Return the pairing holding this code hash."""
    row = (
        await connection.execute(pairings.select().where(pairings.c.code_hash == code_hash))
    ).one_or_none()
    return None if row is None else _to_pairing(row)


async def list_open_for_user(
    connection: AsyncConnection, user_id: str, now: datetime
) -> Sequence[Pairing]:
    """Return the user's pairings that have not finished and have not run out of time."""
    statement = (
        pairings.select()
        .where(pairings.c.user_id == user_id)
        .where(pairings.c.status.in_(OPEN_STATUSES))
        .where(pairings.c.expires_at > now)
        .order_by(pairings.c.created_at)
    )
    return [_to_pairing(row) for row in (await connection.execute(statement)).all()]


async def transition(  # noqa: PLR0913 - one keyword per part of the compare-and-set
    connection: AsyncConnection,
    pairing_id: str,
    *,
    expected: tuple[PairingStatus, ...],
    to: PairingStatus,
    now: datetime,
    values: dict[str, object] | None = None,
    require_unexpired: bool = True,
) -> bool:
    """Move a pairing to ``to`` if it is still in one of ``expected``; say whether it moved.

    ``require_unexpired`` is only turned off for a revocation, which may tidy a pairing
    whose time ran out.
    """
    statement = (
        update(pairings).where(pairings.c.id == pairing_id).where(pairings.c.status.in_(expected))
    )
    if require_unexpired:
        statement = statement.where(pairings.c.expires_at > now)
    result = await connection.execute(statement.values(status=to, **(values or {})))
    return result.rowcount == 1
