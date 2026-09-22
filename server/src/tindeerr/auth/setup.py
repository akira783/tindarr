"""First-run setup: the code, the claim, the wizard's state and the completion.

docs/auth.md, section 3. The service owns the code file's lifecycle, the single active
setup session, and the one transaction that turns "a media server administrator just
signed in from the claiming browser" into a set-up server with its first user and a new
web session.

The sign-in itself is not here: ``complete_setup`` takes the ``MediaUser`` the caller
has already checked against the media server, so step 2b can plug each sign-in method in
without touching this file.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.auth import access, errors
from tindeerr.auth.events import security_event
from tindeerr.auth.mediaserver import MediaServerConnector, MediaServerInput
from tindeerr.auth.methods import AuthMethod, sign_in_methods
from tindeerr.auth.ratelimit import RateLimits
from tindeerr.auth.sessions import CookieGrant, SessionService
from tindeerr.auth.setupcode import (
    generate_setup_code,
    matches,
    read_setup_code,
    remove_setup_code,
    setup_code_hash,
    setup_code_path,
    write_setup_code,
)
from tindeerr.auth.users import link_user
from tindeerr.core.clock import Clock
from tindeerr.ports.media_server import ConnectionCheck, MediaServerKind, MediaUser
from tindeerr.storage import server_state as state_repository
from tindeerr.storage import sessions as session_repository
from tindeerr.storage.db import write_transaction
from tindeerr.storage.sessions import NO_DEVICE, Device, Session
from tindeerr.storage.settings import SettingsStore, as_password_sign_in
from tindeerr.storage.users import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SetupState:
    """What the wizard still has to do (contract schema ``SetupState``)."""

    media_server: MediaServerKind | None
    media_server_locked: bool
    locked_fields: list[str]
    auth_methods: list[AuthMethod]


@dataclass(frozen=True, slots=True)
class CompletedSetup:
    """The result of the sign-in that completed setup: a new web session and its user."""

    grant: CookieGrant
    user: User


class SetupService:
    """Owns the setup code, the claim and the completion."""

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per collaborator, wired by main
        self,
        engine: AsyncEngine,
        data_dir: Path,
        clock: Clock,
        sessions: SessionService,
        connector: MediaServerConnector,
        settings: SettingsStore,
        limits: RateLimits,
    ) -> None:
        self._engine = engine
        self._code_path = setup_code_path(data_dir)
        self._clock = clock
        self._sessions = sessions
        self._connector = connector
        self._settings = settings
        self._limits = limits

    @property
    def code_path(self) -> Path:
        """Where the setup code is written."""
        return self._code_path

    # --- the code -------------------------------------------------------------------

    async def ensure_setup_code(self) -> Path | None:
        """Make sure a pending setup has a usable code; return its path, or ``None``.

        Called at startup. The same code survives a restart; deleting the file (or
        replacing it with something the stored hash does not match) makes a new one.
        Once setup is completed the file is removed, in case a crash left it behind.
        """
        async with self._engine.connect() as connection:
            state = await state_repository.read(connection)
        if state.setup_completed:
            remove_setup_code(self._code_path)
            return None
        current = read_setup_code(self._code_path)
        if state.setup_code_hash is not None and current is not None:
            if matches(current, state.setup_code_hash):
                self._log_code_path()
                return self._code_path
            logger.warning(
                "the setup code file does not match the stored code; writing a new one",
                extra={"path": str(self._code_path)},
            )
        code = generate_setup_code()
        # The file first: a crash before the hash is stored only costs a new code.
        write_setup_code(self._code_path, code)
        async with write_transaction(self._engine) as connection:
            await state_repository.set_setup_code_hash(connection, setup_code_hash(code))
        self._log_code_path()
        return self._code_path

    def _log_code_path(self) -> None:
        logger.info("setup required: enter the code from %s in the web console", self._code_path)

    # --- the wizard -----------------------------------------------------------------

    async def claim(self, code: str, *, client_key: str) -> CookieGrant:
        """Exchange the setup code for a setup session, revoking any earlier one.

        Raises ``setup_completed`` once the server is set up, ``invalid_setup_code`` for
        a wrong code, and ``rate_limited`` past the claim limits.
        """
        self._limits.claim_failures.check(client_key)
        await self._limits.claim_failures_global.admit()
        async with self._engine.connect() as connection:
            state = await state_repository.read(connection)
        if state.setup_completed:
            raise errors.setup_completed()
        if state.setup_code_hash is None or not matches(code, state.setup_code_hash):
            self._limits.claim_failures.record(client_key)
            self._limits.claim_failures_global.record()
            security_event("setup_claim_failed")
            raise errors.invalid_setup_code()
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            await session_repository.revoke_of_kind(connection, "setup", "superseded", now)
            grant = await self._sessions.open_cookie_session(connection, "setup")
        security_event("setup_claimed")
        return grant

    async def state(self, *, client_is_private: bool) -> SetupState:
        """Return what the wizard still has to do for this caller."""
        configured = await self._connector.configured()
        password_sign_in = (await self._settings.get("password_sign_in")).value
        methods = sign_in_methods(
            None if configured is None else configured.kind,
            password_sign_in=as_password_sign_in(password_sign_in),
            client_is_private=client_is_private,
            # Quick Connect needs the media server adapter (step 2b); pairing is not a
            # way to complete setup.
            quick_connect_enabled=None,
            pairing_available=False,
        )
        return SetupState(
            media_server=None if configured is None else configured.kind,
            media_server_locked=self._connector.locked(),
            locked_fields=self._connector.locked_fields(),
            auth_methods=methods,
        )

    async def configure_media_server(
        self, session: Session, request: MediaServerInput
    ) -> ConnectionCheck:
        """Test and save the media server during setup (``PUT /setup/media-server``)."""
        self._limits.connection_tests.hit(session.id)
        async with self._engine.connect() as connection:
            state = await state_repository.read(connection)
        check, _identity = await self._connector.save(
            request, session_id=session.id, install_id=state.install_id
        )
        return check

    async def complete_setup(
        self,
        setup_session: Session,
        media_user: MediaUser,
        *,
        device: Device = NO_DEVICE,
        client_is_private: bool = False,
    ) -> CompletedSetup:
        """Complete first-run setup with the administrator who just signed in.

        In one transaction: the user is created (or linked), setup is marked completed,
        the code hash is cleared, every setup session is revoked and a **new** web
        session is opened, so no session fixation is possible. The code file is deleted
        afterwards, once the transaction has committed.

        Raises ``setup_session_required`` without a live setup session,
        ``setup_required`` when no media server is configured, ``admin_required`` for a
        user who does not administer it, and ``setup_completed`` if another sign-in won
        the race.
        """
        if setup_session.kind != "setup":
            raise errors.setup_session_required()
        if await self._connector.configured() is None:
            raise errors.setup_required()
        if not media_user.is_admin:
            raise errors.admin_required()
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            live = await session_repository.get(connection, setup_session.id)
            if live is None or live.revoked_at is not None or now >= live.expires_at:
                raise errors.setup_session_required()
            user = await link_user(connection, media_user, now)
            access.require_remote_access(user, client_is_private=client_is_private)
            if not await state_repository.complete_setup(connection, now):
                raise errors.setup_completed()
            await session_repository.revoke_of_kind(connection, "setup", "setup_completed", now)
            grant = await self._sessions.open_cookie_session(
                connection, "web", user=user, device=device, reauthenticated=True
            )
        remove_setup_code(self._code_path)
        security_event("setup_completed", user_id=user.id)
        return CompletedSetup(grant, user)
