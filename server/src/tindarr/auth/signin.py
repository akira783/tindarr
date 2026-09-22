"""Signing in through the media server: the checks, the limits and the session opened.

docs/auth.md, section 4. Whatever the method — a password, a Plex PIN, a Quick Connect
code — a sign-in is the same three steps:

1. **is this method available at all?** The configured kind, the ``password_sign_in``
   setting and, for the app, whether first-run setup is done;
2. **prove the account**, against the media server, under the rate limits;
3. **open the session**, which links or creates the Tindarr user, re-reads their
   administrator and remote-access flags from what the media server just said, applies
   the remote-access rule and writes the row.

Step 2 differs per method (the brokered ones live in ``tindarr.auth.brokered``); steps
1 and 3 are here and are shared, so the app and the console cannot drift apart. Nothing
in this module sees a cookie, a header or a request: the API layer passes values.

**The per-username cap** deserves its own note. Tindarr forwards password attempts to
Jellyfin, which disables an account after three failures and only resets that counter on
a success. Left alone, anyone on the Internet could lock out the household's
administrator. So at most two failures per case-folded user name reach the media server
per quarter of an hour; beyond that Tindarr answers ``429`` **without calling it**. It
is a pause, not a lock: it ends on its own, and a successful sign-in clears it.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.auth import access, errors
from tindarr.auth.events import security_event
from tindarr.auth.mediaserver import MediaServerConnector, MediaServerSettings
from tindarr.auth.methods import AuthMethod, password_allowed
from tindarr.auth.ratelimit import RateLimits
from tindarr.auth.sessions import CookieGrant, MobileGrant, SessionService
from tindarr.auth.setup import SetupService
from tindarr.auth.tokens import token_hash
from tindarr.auth.users import link_user
from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import MediaServer, MediaServerKind, MediaUser
from tindarr.storage.db import write_transaction
from tindarr.storage.server_state import ServerStateRepository
from tindarr.storage.sessions import NO_DEVICE, Device, Session
from tindarr.storage.settings import SettingsStore, as_password_sign_in
from tindarr.storage.users import User

#: How long ``GET /QuickConnect/Enabled`` is trusted (docs/auth.md, section 4).
QUICK_CONNECT_CACHE: Final = timedelta(minutes=5)


def username_key(username: str) -> str:
    """Return the key the per-username cap counts under: NFKC, then case-folded.

    Two spellings of the same name must share one bucket, and the key is never logged:
    it is a user name.
    """
    return unicodedata.normalize("NFKC", username).casefold()


@dataclass(frozen=True, slots=True)
class Caller:
    """What the API layer resolved about the client asking to sign in."""

    #: The rate-limit bucket of the resolved client address (docs/auth.md, section 1).
    client_key: str
    client_is_private: bool
    #: True for the web console, which may sign in while setup is still pending.
    console: bool = False
    device: Device = NO_DEVICE


@dataclass(frozen=True, slots=True)
class AppSignIn:
    """An app sign-in: the new mobile session with its first token pair, and its user."""

    grant: MobileGrant
    user: User


@dataclass(frozen=True, slots=True)
class ConsoleSignIn:
    """A console sign-in: the new web session, its user, and whether it completed setup."""

    grant: CookieGrant
    user: User
    setup_completed_now: bool = False


class QuickConnectAvailability:
    """Whether Quick Connect is switched on, remembered for five minutes.

    ``GET /server/info`` must never call the media server, so it reads this cache and
    leaves ``quick_connect`` out while the answer is unknown. A periodic probe and every
    ``POST /auth/quick-connect`` keep it fresh.
    """

    def __init__(self, clock: Clock, ttl: timedelta = QUICK_CONNECT_CACHE) -> None:
        self._clock = clock
        self._ttl = ttl.total_seconds()
        self._enabled: bool | None = None
        self._read_at: float | None = None

    @property
    def cached(self) -> bool | None:
        """The last known answer, or ``None`` while it is unknown or too old."""
        if self._read_at is None or self._clock.monotonic() - self._read_at >= self._ttl:
            return None
        return self._enabled

    def remember(self, *, enabled: bool | None) -> None:
        """Record what the media server answered, or ``None`` to forget it."""
        self._enabled = enabled
        self._read_at = None if enabled is None else self._clock.monotonic()


class SignInService:
    """The rules every sign-in shares, and the password method itself."""

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per collaborator, wired by main
        self,
        engine: AsyncEngine,
        clock: Clock,
        sessions: SessionService,
        connector: MediaServerConnector,
        settings: SettingsStore,
        limits: RateLimits,
        setup: SetupService,
        server_state: ServerStateRepository,
        install_id: str,
        quick_connect: QuickConnectAvailability | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._sessions = sessions
        self._connector = connector
        self._settings = settings
        self._limits = limits
        self._setup = setup
        self._server_state = server_state
        self._install_id = install_id
        self.quick_connect = quick_connect or QuickConnectAvailability(clock)

    # --- what this server offers ----------------------------------------------------

    async def configured(self) -> MediaServerSettings:
        """Return the stored connector, or ``503 setup_required`` when there is none."""
        settings = await self._connector.configured()
        if settings is None:
            raise errors.setup_required()
        return settings

    async def configured_kind(self) -> MediaServerKind | None:
        """Return the configured media server's kind, or ``None`` when there is none."""
        settings = await self._connector.configured()
        return None if settings is None else settings.kind

    async def adapter(self) -> MediaServer:
        """Return the adapter, with its identity checked in the last five minutes."""
        return await self._connector.usable(self._install_id)

    async def require_open(self, caller: Caller) -> None:
        """Refuse an app sign-in before the console has completed first-run setup."""
        if not caller.console and not await self._server_state.setup_completed():
            raise errors.setup_required()

    async def server_name(self) -> str:
        """Return the name this server calls itself (the plex.tv approval page shows it)."""
        name = (await self._settings.get("server_name")).value
        return name if isinstance(name, str) and name else "Tindarr"

    async def password_sign_in_allowed(self, *, client_is_private: bool) -> bool:
        """Whether the ``password_sign_in`` setting offers passwords to this caller."""
        setting = as_password_sign_in((await self._settings.get("password_sign_in")).value)
        return password_allowed(setting, client_is_private=client_is_private)

    # --- the password method --------------------------------------------------------

    async def password(self, username: str, password: str, caller: Caller) -> MediaUser:
        """Check a password with the media server, under both password limits.

        An unknown user and a wrong password give exactly the same ``401``, and both
        count the same way, so the answer says nothing about which accounts exist.
        """
        await self.require_open(caller)
        settings = await self.configured()
        if settings.kind == "plex":
            raise errors.sign_in_method_unavailable(
                "This server signs in with a Plex PIN, not with a password."
            )
        if not await self.password_sign_in_allowed(client_is_private=caller.client_is_private):
            raise errors.password_sign_in_disabled()
        key = username_key(username)
        # Checked before the media server is called at all: that is the whole point.
        self._limits.password_per_username.check(key)
        await self._limits.password_failures.wait(caller.client_key)
        adapter = await self.adapter()
        try:
            user = await adapter.authenticate_password(username, password)
        except ProblemError as problem:
            self._record_failure(problem, key, caller)
            raise
        self._limits.password_per_username.forget(key)
        self._limits.password_failures.forget(caller.client_key)
        return user

    def _record_failure(self, problem: ProblemError, key: str, caller: Caller) -> None:
        if problem.code != "invalid_credentials":
            # A disabled account or an unreachable server is not a wrong password, and
            # must not fill the quota that protects the media server's lockout counter.
            return
        self._limits.password_per_username.record(key)
        self._limits.password_failures.record_failure(caller.client_key)
        # The name is hashed: a failure line must not name an account that may not exist.
        security_event(
            "sign_in_failed",
            method="password",
            username_hash=token_hash(key)[:16],
            client=caller.client_key,
        )

    # --- opening the session --------------------------------------------------------

    async def open_app_session(
        self, media_user: MediaUser, caller: Caller, method: AuthMethod
    ) -> AppSignIn:
        """Link the account and open a ``mobile`` session with its first token pair."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            user = await link_user(connection, media_user, now)
            access.require_remote_access(user, client_is_private=caller.client_is_private)
            grant = await self._sessions.open_mobile_session(connection, user, caller.device)
        self._signed_in(method, user, caller, kind="app")
        return AppSignIn(grant, user)

    async def open_console_session(
        self, media_user: MediaUser, caller: Caller, method: AuthMethod, setup: Session | None
    ) -> ConsoleSignIn:
        """Open a ``web`` session, completing first-run setup when it is still pending.

        While setup is pending the request must carry the setup session that claimed the
        server, and the account must administer the media server: that sign-in is what
        completes setup, and it gets a brand-new session so nothing can be fixated.
        """
        if not await self._server_state.setup_completed():
            if setup is None:
                raise errors.setup_session_required()
            completed = await self._setup.complete_setup(
                setup,
                media_user,
                device=caller.device,
                client_is_private=caller.client_is_private,
            )
            self._signed_in(method, completed.user, caller, kind="console")
            return ConsoleSignIn(completed.grant, completed.user, setup_completed_now=True)
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            user = await link_user(connection, media_user, now)
            access.require_remote_access(user, client_is_private=caller.client_is_private)
            grant = await self._sessions.open_cookie_session(
                connection, "web", user=user, device=caller.device, reauthenticated=True
            )
        self._signed_in(method, user, caller, kind="console")
        return ConsoleSignIn(grant, user)

    @staticmethod
    def _signed_in(method: AuthMethod, user: User, caller: Caller, *, kind: str) -> None:
        security_event(
            "sign_in",
            method=method,
            client_kind=kind,
            user_id=user.id,
            client=caller.client_key,
            admin=user.is_admin,
        )

    # --- step-up re-authentication --------------------------------------------------

    async def complete_reauth(
        self, session: Session, session_user: User, media_user: MediaUser, caller: Caller
    ) -> datetime:
        """Record a fresh re-authentication and refresh what the media server just said.

        The account proved must be the session's own: another account of the same media
        server is not a step-up, it is somebody else, and answers ``invalid_credentials``.
        """
        if session_user.media_server_user_id != media_user.id:
            raise errors.invalid_credentials()
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            user = await link_user(connection, media_user, now)
            access.require_remote_access(user, client_is_private=caller.client_is_private)
        return await self._sessions.mark_reauthenticated(session.id)
