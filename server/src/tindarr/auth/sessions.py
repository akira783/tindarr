"""Sessions: opening them, authenticating a request, rotating tokens, purging.

The rules of docs/auth.md, sections 2 and 7:

- a session and its user are loaded on **every** authenticated request, so a revocation
  or a disable takes effect at once;
- lifetimes are both idle and absolute (app 60 days idle / 90 days, console 24 hours /
  7 days, setup 30 minutes);
- refresh rotation is an atomic compare-and-set with strict reuse detection: presenting
  a used token revokes the whole session, with no grace period (docs/adr/0010).

Nothing here sees a cookie or a header: the API layer extracts the values and calls in.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tindarr.auth import access, errors
from tindarr.auth.events import security_event
from tindarr.auth.tokens import AccessTokens, new_token, token_hash
from tindarr.core.clock import Clock
from tindarr.storage import sessions as repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.sessions import (
    NO_DEVICE,
    Device,
    RevokedReason,
    Session,
    SessionKind,
    SessionLifetime,
)
from tindarr.storage.users import User

#: Absolute lifetimes, from the moment the session is opened.
ABSOLUTE_LIFETIMES: Final[Mapping[SessionKind, timedelta]] = {
    "mobile": timedelta(days=90),
    "web": timedelta(days=7),
    "setup": timedelta(minutes=30),
}
#: Idle timeouts; a setup session only has its absolute one.
IDLE_TIMEOUTS: Final[Mapping[SessionKind, timedelta | None]] = {
    "mobile": timedelta(days=60),
    "web": timedelta(hours=24),
    "setup": None,
}
REFRESH_TOKEN_LIFETIME: Final = timedelta(days=60)
#: ``last_seen_at`` is written at most this often, to keep reads from writing every time.
LAST_SEEN_INTERVAL: Final = timedelta(minutes=1)
#: The daily purge keeps finished sessions and old pairings this long.
PURGE_SESSION_AGE: Final = timedelta(days=30)
PURGE_PAIRING_AGE: Final = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Authenticated:
    """A request's credential, resolved: its session and, unless it is a setup one, its user."""

    session: Session
    user: User | None

    @property
    def kind(self) -> SessionKind:
        """The session's kind."""
        return self.session.kind

    def require_user(self) -> User:
        """Return the user, or fail with ``unauthorized`` for a setup session."""
        if self.user is None:
            raise errors.unauthorized("This credential has no user.")
        return self.user


@dataclass(frozen=True, slots=True)
class CookieGrant:
    """A new cookie session: the row, and the token the browser must be given once."""

    session: Session
    token: str

    @property
    def csrf_token(self) -> str:
        """The session's CSRF token (every cookie session has one)."""
        return self.session.csrf_token or ""


@dataclass(frozen=True, slots=True)
class TokenPair:
    """The app's credentials: a short-lived access token and a rotating refresh token."""

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class MobileGrant:
    """A new app session and its first token pair."""

    session: Session
    tokens: TokenPair


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What the daily purge deleted."""

    sessions: int
    pairings: int


class SessionService:
    """Opens, checks and closes sessions of all three kinds."""

    def __init__(self, engine: AsyncEngine, clock: Clock, access_tokens: AccessTokens) -> None:
        self._engine = engine
        self._clock = clock
        self._access_tokens = access_tokens

    @property
    def clock(self) -> Clock:
        """The clock this service decides lifetimes with."""
        return self._clock

    # --- opening sessions (inside the caller's transaction) -------------------------

    async def open_cookie_session(
        self,
        connection: AsyncConnection,
        kind: SessionKind,
        *,
        user: User | None = None,
        device: Device = NO_DEVICE,
        reauthenticated: bool = False,
    ) -> CookieGrant:
        """Open a ``web`` or ``setup`` session and return it with its cookie token."""
        now = self._clock.now()
        token = new_token()
        session = repository.new_session(
            kind,
            SessionLifetime(
                now,
                now + ABSOLUTE_LIFETIMES[kind],
                token_hash=token_hash(token),
                csrf_token=new_token(),
                reauth_at=now if reauthenticated else None,
            ),
            user_id=None if user is None else user.id,
            device=device,
        )
        await repository.insert(connection, session)
        security_event("session_opened", kind=kind, user_id=None if user is None else user.id)
        return CookieGrant(session, token)

    async def open_mobile_session(
        self, connection: AsyncConnection, user: User, device: Device
    ) -> MobileGrant:
        """Open an app session and issue its first token pair."""
        now = self._clock.now()
        session = repository.new_session(
            "mobile",
            SessionLifetime(now, now + ABSOLUTE_LIFETIMES["mobile"]),
            user_id=user.id,
            device=device,
        )
        await repository.insert(connection, session)
        tokens = await self._issue_pair(connection, session, now)
        security_event("session_opened", kind="mobile", user_id=user.id)
        return MobileGrant(session, tokens)

    async def _issue_pair(
        self, connection: AsyncConnection, session: Session, now: datetime
    ) -> TokenPair:
        issued = self._access_tokens.issue(session.user_id or "", session.id)
        refresh = new_token()
        expires_at = min(now + REFRESH_TOKEN_LIFETIME, session.expires_at)
        await repository.add_refresh_token(
            connection,
            session_id=session.id,
            token_hash=token_hash(refresh),
            now=now,
            expires_at=expires_at,
        )
        return TokenPair(issued.value, issued.expires_at, refresh, expires_at)

    # --- authenticating a request ---------------------------------------------------

    async def authenticate_cookie(
        self, token: str, kinds: tuple[SessionKind, ...]
    ) -> Authenticated:
        """Resolve a cookie token into a live session of one of ``kinds``, with its user."""
        async with self._engine.connect() as connection:
            session = await repository.get_by_token_hash(connection, token_hash(token))
            if session is None or session.kind not in kinds:
                raise errors.unauthorized()
            authenticated = await self._check(connection, session)
        await self._touch(session, authenticated.user)
        return authenticated

    async def authenticate_access_token(self, token: str) -> Authenticated:
        """Verify an access token and load its session and user."""
        claims = self._access_tokens.verify(token)
        async with self._engine.connect() as connection:
            session = await repository.get(connection, claims.session_id)
            if session is None or session.kind != "mobile" or session.user_id != claims.user_id:
                raise errors.unauthorized()
            authenticated = await self._check(connection, session)
        await self._touch(session, authenticated.user)
        return authenticated

    async def _check(self, connection: AsyncConnection, session: Session) -> Authenticated:
        self._require_live(session)
        if session.user_id is None:
            return Authenticated(session, None)
        user = await user_repository.get(connection, session.user_id)
        if user is None:
            raise errors.unauthorized()
        access.require_usable_account(user)
        return Authenticated(session, user)

    def _require_live(self, session: Session) -> None:
        now = self._clock.now()
        if session.revoked_at is not None or now >= session.expires_at:
            raise errors.unauthorized()
        idle = IDLE_TIMEOUTS[session.kind]
        if idle is not None and now - session.last_seen_at >= idle:
            raise errors.unauthorized()

    async def _touch(self, session: Session, user: User | None) -> None:
        now = self._clock.now()
        if now - session.last_seen_at < LAST_SEEN_INTERVAL:
            return
        async with write_transaction(self._engine) as connection:
            await repository.touch(connection, session.id, now)
            if user is not None:
                await user_repository.touch(connection, user.id, now)

    # --- changing sessions ----------------------------------------------------------

    async def revoke(self, session_id: str, reason: RevokedReason) -> bool:
        """Revoke one session; ``False`` when it was already revoked."""
        async with write_transaction(self._engine) as connection:
            revoked = await repository.revoke(connection, session_id, reason, self._clock.now())
        if revoked:
            security_event("session_revoked", reason=reason)
        return revoked

    async def revoke_user_sessions(
        self, user_id: str, reason: RevokedReason, except_id: str | None = None
    ) -> int:
        """Revoke a user's sessions, optionally keeping the caller's own."""
        async with write_transaction(self._engine) as connection:
            count = await repository.revoke_for_user(
                connection, user_id, reason, self._clock.now(), except_id
            )
        if count:
            security_event("sessions_revoked", reason=reason, user_id=user_id, count=count)
        return count

    async def mark_reauthenticated(self, session_id: str) -> datetime:
        """Record a fresh re-authentication and return when it stops counting."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            await repository.mark_reauthenticated(connection, session_id, now)
        security_event("reauthenticated")
        return now + access.REAUTH_WINDOW

    async def rotate(self, refresh_token: str, *, client_is_private: bool) -> TokenPair:
        """Exchange a refresh token for a new pair (docs/auth.md, section 7).

        The compare-and-set either claims the token or finds it used: a used token means
        theft or a client that did not refresh single-flight, and revokes the session.
        """
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            outcome = await repository.claim_refresh_token(
                connection, token_hash(refresh_token), now
            )
            if outcome.session_id is None:
                raise errors.unauthorized("Unknown or expired refresh token.")
            if outcome.claimed:
                return await self._rotate_claimed(
                    connection, outcome.session_id, now, client_is_private=client_is_private
                )
            # Reuse: revoke inside this transaction and report it once it has committed.
            await repository.revoke(connection, outcome.session_id, "reuse", now)
        security_event("refresh_token_reused", session_revoked=True)
        raise errors.refresh_token_reused()

    async def _rotate_claimed(
        self,
        connection: AsyncConnection,
        session_id: str,
        now: datetime,
        *,
        client_is_private: bool,
    ) -> TokenPair:
        session = await repository.get(connection, session_id)
        if session is None or session.kind != "mobile":
            raise errors.unauthorized()
        self._require_live(session)
        user = await user_repository.get(connection, session.user_id or "")
        if user is None:
            raise errors.unauthorized()
        access.require_usable_account(user)
        access.require_remote_access(user, client_is_private=client_is_private)
        await repository.touch(connection, session.id, now)
        return await self._issue_pair(connection, session, now)

    # --- housekeeping ---------------------------------------------------------------

    async def purge(self) -> PurgeResult:
        """Delete sessions finished more than 30 days ago and pairings older than 7 days."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            removed_sessions, removed_pairings = await repository.purge(
                connection,
                sessions_before=now - PURGE_SESSION_AGE,
                pairings_before=now - PURGE_PAIRING_AGE,
            )
        if removed_sessions or removed_pairings:
            security_event("purged", sessions=removed_sessions, pairings=removed_pairings)
        return PurgeResult(removed_sessions, removed_pairings)
