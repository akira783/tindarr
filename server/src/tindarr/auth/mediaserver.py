"""The media server connector: reading it, testing it and saving it.

Used by first-run setup (``PUT /setup/media-server``) and, from step 2c, by the admin
connector endpoint, which adds the rules that only apply after setup (media server
administrator, fresh re-authentication, identity change). What lives here is what both
share (docs/auth.md, section 6):

- the settings that describe the connection, and which of them the environment locks;
- the rule that a stored secret is only reused for the same kind and URL;
- the connection test, whose result is coarse, and the identity that is stored with it.

The adapter itself comes from a ``MediaServerFactory`` (step 2b wires the real ones in),
and the Plex owner token from an ``OwnerTokenHandles`` registry, which holds the Plex
PIN handles (also step 2b).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.auth import errors
from tindarr.auth.events import security_event
from tindarr.core.clock import Clock, SystemClock
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import (
    ConnectionCheck,
    MediaServer,
    MediaServerConnection,
    MediaServerFactory,
    MediaServerKind,
    ServerIdentity,
    as_media_server_kind,
)
from tindarr.storage import server_state as state_repository
from tindarr.storage import sessions as session_repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.settings import SettingLockedError, SettingsStore

logger = logging.getLogger(__name__)

#: Contract field of ``MediaServerConfigInput`` -> the setting that stores it.
SETTING_FOR_FIELD: Final[Mapping[str, str]] = {
    "server_type": "media_server_kind",
    "url": "media_server_url",
    "api_key": "media_server_api_key",
    "verify_tls": "media_server_verify_tls",
}
#: The fields that must all be set in the environment for the wizard to skip the step.
LOCKING_FIELDS: Final = ("server_type", "url", "api_key")
#: How long the identity read before a sign-in stays good for (docs/auth.md, section 6).
IDENTITY_CACHE: Final = timedelta(minutes=5)


class OwnerTokenHandles(Protocol):
    """The Plex PIN handles of purpose ``owner_token`` (docs/auth.md, section 5).

    Step 2b implements it on the in-memory handle registry. ``peek`` returns the token
    of an approved PIN without consuming the handle, so a failed connection test leaves
    it usable; ``consume`` is called once the connector is saved.
    """

    async def peek(self, handle: str, session_id: str) -> str:
        """Return the owner token, or raise ``pin_expired`` / ``plex_pin_pending``."""
        ...

    async def consume(self, handle: str, session_id: str) -> None:
        """Mark the handle used, once the token has been stored."""
        ...


class NoOwnerTokenHandles:
    """Stand-in until step 2b brings the Plex PIN flow: every handle is unknown."""

    async def peek(self, handle: str, session_id: str) -> str:
        """Raise ``pin_expired``: no handle can exist yet."""
        raise errors.pin_expired()

    async def consume(self, handle: str, session_id: str) -> None:
        """Raise ``pin_expired``: there is nothing to consume."""
        raise errors.pin_expired()


@dataclass(frozen=True, slots=True)
class MediaServerInput:
    """A ``MediaServerConfigInput`` from the contract, as values.

    ``given`` lists the fields the request actually carried, so an omitted field is
    neither checked against a lock nor written.
    """

    kind: MediaServerKind
    url: str
    api_key: str | None = None
    plex_pin_id: str | None = None
    verify_tls: bool = True
    given: frozenset[str] = field(default_factory=frozenset[str])


@dataclass(frozen=True, slots=True)
class LockedMediaServerValues:
    """What an environment variable forces, among the fields it is safe to show.

    The wizard needs them: ``PUT /setup/media-server`` takes the whole connector, so a
    step that cannot show a locked URL can only tell the administrator "it must match".
    The API key is deliberately absent — a locked secret is shown as locked, never
    returned.
    """

    server_type: MediaServerKind | None = None
    url: str | None = None
    verify_tls: bool | None = None


@dataclass(frozen=True, slots=True)
class SavedConnector:
    """What saving the connector produced: the test, the identity, and whether it moved."""

    check: ConnectionCheck
    identity: ServerIdentity
    #: True when the saved server is not the one Tindarr was set up with.
    identity_changed: bool


@dataclass(frozen=True, slots=True)
class MediaServerSettings:
    """The stored connector, as the adapters need it."""

    kind: MediaServerKind
    url: str
    secret: str
    verify_tls: bool


class MediaServerConnector:
    """Reads, tests and saves the media server connector."""

    def __init__(
        self,
        engine: AsyncEngine,
        settings: SettingsStore,
        factory: MediaServerFactory,
        owner_tokens: OwnerTokenHandles | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._factory = factory
        self._owner_tokens = owner_tokens or NoOwnerTokenHandles()
        self._clock = clock or SystemClock()
        self._identity_checked_at: float | None = None

    # --- reading --------------------------------------------------------------------

    async def configured(self) -> MediaServerSettings | None:
        """Return the stored connector, or ``None`` while it is incomplete."""
        kind = (await self._settings.get("media_server_kind")).value
        url = (await self._settings.get("media_server_url")).value
        secret = (await self._settings.get("media_server_api_key")).value
        verify_tls = (await self._settings.get("media_server_verify_tls")).value
        if not isinstance(kind, str) or not isinstance(url, str) or not isinstance(secret, str):
            return None
        media_kind = as_media_server_kind(kind)
        if media_kind is None:  # pragma: no cover - the setting type already checks this
            return None
        return MediaServerSettings(media_kind, url, secret, bool(verify_tls))

    def locked_fields(self) -> list[str]:
        """Contract fields set by an environment variable (shown as locked, and refused)."""
        return [
            field_name
            for field_name, setting in SETTING_FOR_FIELD.items()
            if self._settings.is_locked(setting)
        ]

    def locked_values(self) -> LockedMediaServerValues:
        """Return the values the environment forces, for the fields the console may show."""
        url = self._settings.locked_value("media_server_url")
        verify_tls = self._settings.locked_value("media_server_verify_tls")
        return LockedMediaServerValues(
            server_type=as_media_server_kind(self._settings.locked_value("media_server_kind")),
            url=url if isinstance(url, str) else None,
            verify_tls=verify_tls if isinstance(verify_tls, bool) else None,
        )

    def locked(self) -> bool:
        """Whether the environment sets kind, URL and secret, so the wizard skips the step."""
        locked = set(self.locked_fields())
        return all(name in locked for name in LOCKING_FIELDS)

    async def connect(self, install_id: str) -> MediaServer:
        """Build the adapter for the stored connector, or fail with ``setup_required``."""
        settings = await self.configured()
        if settings is None:
            raise errors.setup_required()
        return self._adapter(settings, install_id)

    async def usable(self, install_id: str) -> MediaServer:
        """Return the adapter, having checked recently that it is still the same server.

        The identity is re-read at most every five minutes before a sign-in uses the
        media server (docs/auth.md, section 6). A server that no longer answers with the
        stored identity — reinstalled, replaced, or something else at that address —
        stops every sign-in with ``media_server_changed`` until an administrator or the
        operator acts. Tindarr never re-links on its own.
        """
        adapter = await self.connect(install_id)
        now = self._clock.monotonic()
        if (
            self._identity_checked_at is not None
            and now - self._identity_checked_at < IDENTITY_CACHE.total_seconds()
        ):
            return adapter
        async with self._engine.connect() as connection:
            stored = (await state_repository.read(connection)).media_server_identity
        identity = await adapter.identify()
        if stored is not None and identity.key != stored:
            security_event("media_server_identity_mismatch", kind=identity.kind)
            raise errors.media_server_changed()
        self._identity_checked_at = now
        return adapter

    def forget_identity_check(self) -> None:
        """Make the next ``usable`` re-read the identity (the connector just changed)."""
        self._identity_checked_at = None

    def _adapter(self, settings: MediaServerSettings, install_id: str) -> MediaServer:
        return self._factory(
            MediaServerConnection(
                kind=settings.kind,
                url=settings.url,
                secret=settings.secret,
                verify_tls=settings.verify_tls,
                device_id=install_id,
            )
        )

    # --- saving ---------------------------------------------------------------------

    async def check(
        self, request: MediaServerInput, *, session_id: str, install_id: str
    ) -> ConnectionCheck:
        """Test a connection without saving anything (``…/media_server/test``).

        The owner-token handle is only read, never consumed: the administrator may have
        to correct the URL and try again with the same PIN.
        """
        self._check_locks(request)
        secret = await self._secret_for(request, session_id=session_id)
        settings = MediaServerSettings(request.kind, request.url, secret, request.verify_tls)
        return await self._adapter(settings, install_id).test()

    async def save(
        self,
        request: MediaServerInput,
        *,
        session_id: str,
        install_id: str,
        relink: bool = False,
    ) -> SavedConnector:
        """Test the connection and store it; report the test, the identity and a change.

        ``relink`` turns on the identity-change rules of docs/auth.md, section 6: when
        the saved server is not the one Tindarr was set up with, the same transaction
        revokes **every** session and unlinks every user, so nobody keeps access to
        someone else's account on the new server. First-run setup passes ``False``: no
        user exists yet, and revoking would only close the wizard's own session.

        Raises ``setting_locked`` for a field the environment sets to another value,
        ``secret_required`` when the stored secret cannot be reused, the Plex PIN
        problems, and ``502`` when the test fails (nothing is then saved).
        """
        self._check_locks(request)
        secret = await self._secret_for(request, session_id=session_id)
        settings = MediaServerSettings(request.kind, request.url, secret, request.verify_tls)
        check = await self._adapter(settings, install_id).test()
        if not check.ok:
            raise errors.connector_failed(check.health)
        identity = await self._adapter(settings, install_id).identify()
        if identity.kind != request.kind:
            raise errors.media_server_unsupported(
                "That address answers as another kind of media server."
            )
        changed = await self._store(settings, identity, relink=relink)
        if request.plex_pin_id is not None:
            await self._spend_pin(request.plex_pin_id, session_id)
        self.forget_identity_check()
        security_event("media_server_saved", kind=request.kind, identity_changed=changed)
        return SavedConnector(check, identity, changed)

    async def _spend_pin(self, handle: str, session_id: str) -> None:
        """Consume the owner-token PIN, tolerating one that expired in the meantime.

        The connector is already stored at this point. A handle whose ten minutes ran
        out during the connection test is simply gone, and answering ``410`` — which
        the contract reads as "nothing happened" — would be a lie.
        """
        try:
            await self._owner_tokens.consume(handle, session_id)
        except ProblemError as problem:
            logger.warning(
                "the Plex PIN was already gone when the connector was saved",
                extra={"problem": problem.code},
            )

    def _check_locks(self, request: MediaServerInput) -> None:
        values: Mapping[str, object] = {
            "server_type": request.kind,
            "url": request.url,
            "api_key": request.api_key,
            "verify_tls": request.verify_tls,
        }
        for field_name in request.given & set(SETTING_FOR_FIELD):
            setting = SETTING_FOR_FIELD[field_name]
            if not self._settings.is_locked(setting):
                continue
            if values[field_name] != self._settings.locked_value(setting):
                raise SettingLockedError(setting)

    async def _secret_for(self, request: MediaServerInput, *, session_id: str) -> str:
        locked = self._settings.locked_value("media_server_api_key")
        if isinstance(locked, str):
            # The environment holds the API key (or the Plex owner token): it wins.
            return locked
        if request.plex_pin_id is not None:
            return await self._owner_tokens.peek(request.plex_pin_id, session_id)
        if request.api_key is not None:
            return request.api_key
        return await self._kept_secret(request)

    async def _kept_secret(self, request: MediaServerInput) -> str:
        """Reuse the stored secret only when the kind and the URL are unchanged."""
        current = await self.configured()
        if current is None or current.kind != request.kind or current.url != request.url:
            raise errors.secret_required()
        return current.secret

    async def _store(
        self, settings: MediaServerSettings, identity: ServerIdentity, *, relink: bool
    ) -> bool:
        values: dict[str, object] = {
            "media_server_kind": settings.kind,
            "media_server_url": settings.url,
            "media_server_api_key": settings.secret,
            "media_server_verify_tls": settings.verify_tls,
            # So the console can say "connected to Home Jellyfin" without testing again.
            "media_server_name": identity.name,
        }
        unlocked = {
            name: value for name, value in values.items() if not self._settings.is_locked(name)
        }
        # The connector and the identity it was read from are written together: a
        # configured connector without its identity would stop every sign-in (section 6).
        async with write_transaction(self._engine) as connection:
            stored = (await state_repository.read(connection)).media_server_identity
            changed = stored is not None and stored != identity.key
            await self._settings.set_many(unlocked, connection)
            await state_repository.set_media_server_identity(connection, identity.key)
            if changed and relink:
                now = self._clock.now()
                revoked = await session_repository.revoke_all(connection, "server_changed", now)
                unlinked = await user_repository.unlink_all(connection)
                security_event("media_server_replaced", sessions=revoked, users=unlinked)
        return changed
