"""The ``sessions`` and ``refresh_tokens`` tables.

Rows and queries only: what a session's lifetime is, and what to do when a refresh token
comes back twice, is decided in ``tindarr.auth``. As in ``tindarr.storage.users``,
every function takes its connection so callers can compose them in one transaction.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import Row, Update, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from tindarr.storage.ids import new_id
from tindarr.storage.tables import pairings, refresh_tokens, sessions

type SessionKind = Literal["mobile", "web", "setup"]
type RevokedReason = Literal[
    "logout",
    "user",
    "admin",
    "disabled",
    "reuse",
    "setup_completed",
    "superseded",
    "server_changed",
]


@dataclass(frozen=True, slots=True)
class Device:
    """How a session describes the client it belongs to."""

    name: str | None = None
    platform: str | None = None
    app_version: str | None = None


#: A session with no device of its own (the setup session).
NO_DEVICE: Final = Device()


@dataclass(frozen=True, slots=True)
class Session:
    """One row of ``sessions``."""

    id: str
    kind: SessionKind
    user_id: str | None
    token_hash: str | None
    csrf_token: str | None
    device_name: str | None
    platform: str | None
    app_version: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    reauth_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: RevokedReason | None

    @property
    def device(self) -> Device:
        """The client this session belongs to."""
        return Device(self.device_name, self.platform, self.app_version)


def _to_session(row: Row[tuple[Any, ...]]) -> Session:
    # Positional: ``Session`` lists the columns of ``sessions`` in order (a test checks it).
    return Session(*row)


def new_session(
    kind: SessionKind,
    lifetime: "SessionLifetime",
    *,
    user_id: str | None = None,
    device: Device = NO_DEVICE,
) -> Session:
    """Build a session row (not stored yet). ``tindarr.auth`` owns the lifetimes."""
    return Session(
        id=new_id(),
        kind=kind,
        user_id=user_id,
        token_hash=lifetime.token_hash,
        csrf_token=lifetime.csrf_token,
        device_name=device.name,
        platform=device.platform,
        app_version=device.app_version,
        created_at=lifetime.now,
        last_seen_at=lifetime.now,
        expires_at=lifetime.expires_at,
        reauth_at=lifetime.reauth_at,
        revoked_at=None,
        revoked_reason=None,
    )


@dataclass(frozen=True, slots=True)
class SessionLifetime:
    """When a new session starts and ends, and the cookie credential it carries."""

    now: datetime
    expires_at: datetime
    token_hash: str | None = None
    csrf_token: str | None = None
    reauth_at: datetime | None = None


async def insert(connection: AsyncConnection, session: Session) -> Session:
    """Store a new session row and return it."""
    await connection.execute(sessions.insert().values(**asdict(session)))
    return session


async def get(connection: AsyncConnection, session_id: str) -> Session | None:
    """Return the session with this id."""
    row = (
        await connection.execute(sessions.select().where(sessions.c.id == session_id))
    ).one_or_none()
    return None if row is None else _to_session(row)


async def get_by_token_hash(connection: AsyncConnection, token_hash: str) -> Session | None:
    """Return the cookie session holding this token hash."""
    row = (
        await connection.execute(sessions.select().where(sessions.c.token_hash == token_hash))
    ).one_or_none()
    return None if row is None else _to_session(row)


async def list_for_user(connection: AsyncConnection, user_id: str) -> Sequence[Session]:
    """Return a user's live sessions, most recent first."""
    statement = (
        sessions.select()
        .where(sessions.c.user_id == user_id)
        .where(sessions.c.revoked_at.is_(None))
        .order_by(sessions.c.created_at.desc())
    )
    return [_to_session(row) for row in (await connection.execute(statement)).all()]


async def touch(connection: AsyncConnection, session_id: str, now: datetime) -> None:
    """Record that the session was used now."""
    await connection.execute(
        update(sessions).where(sessions.c.id == session_id).values(last_seen_at=now)
    )


async def mark_reauthenticated(connection: AsyncConnection, session_id: str, now: datetime) -> None:
    """Record a fresh re-authentication on a web session (docs/auth.md, section 7)."""
    await connection.execute(
        update(sessions).where(sessions.c.id == session_id).values(reauth_at=now)
    )


async def revoke(
    connection: AsyncConnection, session_id: str, reason: RevokedReason, now: datetime
) -> bool:
    """Revoke one session; ``False`` when it was already revoked (compare-and-set)."""
    result = await connection.execute(
        update(sessions)
        .where(sessions.c.id == session_id)
        .where(sessions.c.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    return result.rowcount == 1


async def _revoke_matching(
    connection: AsyncConnection, statement: Update, reason: RevokedReason, now: datetime
) -> int:
    result = await connection.execute(
        statement.where(sessions.c.revoked_at.is_(None)).values(
            revoked_at=now, revoked_reason=reason
        )
    )
    return result.rowcount


async def revoke_all(connection: AsyncConnection, reason: RevokedReason, now: datetime) -> int:
    """Revoke every live session (a media server change, or the CLI reset)."""
    return await _revoke_matching(connection, update(sessions), reason, now)


async def revoke_of_kind(
    connection: AsyncConnection, kind: SessionKind, reason: RevokedReason, now: datetime
) -> int:
    """Revoke every live session of one kind (a new claim supersedes the setup session)."""
    return await _revoke_matching(
        connection, update(sessions).where(sessions.c.kind == kind), reason, now
    )


async def revoke_for_user(
    connection: AsyncConnection,
    user_id: str,
    reason: RevokedReason,
    now: datetime,
    except_id: str | None = None,
) -> int:
    """Revoke a user's live sessions, optionally keeping the caller's own."""
    statement = update(sessions).where(sessions.c.user_id == user_id)
    if except_id is not None:
        statement = statement.where(sessions.c.id != except_id)
    return await _revoke_matching(connection, statement, reason, now)


async def add_refresh_token(
    connection: AsyncConnection,
    *,
    session_id: str,
    token_hash: str,
    now: datetime,
    expires_at: datetime,
) -> None:
    """Store a new refresh token for a mobile session."""
    await connection.execute(
        refresh_tokens.insert().values(
            token_hash=token_hash, session_id=session_id, created_at=now, expires_at=expires_at
        )
    )


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What the compare-and-set on a refresh token found.

    ``session_id`` is set when the token was claimed (``claimed``) or when a used token
    came back (``reused``); both cases name the session it belongs to.
    """

    claimed: bool
    reused: bool
    session_id: str | None = None


async def claim_refresh_token(
    connection: AsyncConnection, token_hash: str, now: datetime
) -> RefreshOutcome:
    """Mark a refresh token used, atomically, and say what happened.

    Must run inside a write transaction (``BEGIN IMMEDIATE``): exactly one of two
    concurrent rotations of the same token claims it, the other sees it used.
    """
    result = await connection.execute(
        update(refresh_tokens)
        .where(refresh_tokens.c.token_hash == token_hash)
        .where(refresh_tokens.c.used_at.is_(None))
        .where(refresh_tokens.c.expires_at > now)
        .values(used_at=now)
    )
    row = (
        await connection.execute(
            select(refresh_tokens.c.session_id, refresh_tokens.c.used_at).where(
                refresh_tokens.c.token_hash == token_hash
            )
        )
    ).one_or_none()
    if row is None:
        return RefreshOutcome(claimed=False, reused=False)
    if result.rowcount == 1:
        return RefreshOutcome(claimed=True, reused=False, session_id=row.session_id)
    if row.used_at is not None:
        # The token was already rotated: a theft, or a client that did not refresh
        # single-flight. Either way the session goes (docs/adr/0010).
        return RefreshOutcome(claimed=False, reused=True, session_id=row.session_id)
    return RefreshOutcome(claimed=False, reused=False)


async def purge(
    connection: AsyncConnection, *, sessions_before: datetime, pairings_before: datetime
) -> tuple[int, int]:
    """Delete finished sessions and old pairings; return how many of each.

    A session goes once it has been revoked or past its absolute end for long enough; its
    refresh tokens go with it (foreign key cascade), including the used ones kept for
    reuse detection.
    """
    removed_sessions = await connection.execute(
        delete(sessions).where(
            or_(
                sessions.c.revoked_at < sessions_before,
                sessions.c.expires_at < sessions_before,
            )
        )
    )
    removed_pairings = await connection.execute(
        delete(pairings).where(pairings.c.created_at < pairings_before)
    )
    return removed_sessions.rowcount, removed_pairings.rowcount
