"""The two brokered sign-ins: Plex PINs and Jellyfin Quick Connect.

Both work the same way from the client's side: ask for something to show the user, then
poll until they have approved it somewhere else. Tindeerr stands in the middle so the
client never touches plex.tv or the media server itself, and never sees the upstream
credential — only a handle (``tindeerr.auth.handles``).

What each flow must get right:

- **Plex.** A PIN proves an account, not access. The account may sign in only when a
  plex.tv resource matches the stored ``machineIdentifier``, and administers the server
  only when ``owned`` is true **on that resource**. Names and advertised URLs are
  self-reported and decide nothing. Sign-in PINs use a client identifier of their own,
  so deleting the device the approval created — done right after the access check —
  can never revoke the install's own owner token.
- **Quick Connect.** Jellyfin opens the user's session the moment they approve the
  code, not when Tindeerr collects it. Collecting therefore also ends that session, and
  a sweep collects approvals nobody came back for (``sweep_quick_connect``).

Polling is throttled to one upstream call per second per handle; a faster client gets
``202`` and the same answer a moment later.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from tindeerr.auth import errors
from tindeerr.auth.handles import Binding, HandlePurpose, HandleRegistry
from tindeerr.auth.signin import Caller, SignInService
from tindeerr.core.errors import PendingError, ProblemError
from tindeerr.ports.media_server import MediaUser, ServerIdentity
from tindeerr.ports.plextv import PlexTv, as_media_user, find_server
from tindeerr.storage.server_state import ServerStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StartedPin:
    """What the client needs to have a Plex PIN approved."""

    handle: str
    auth_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PinStatus:
    """The read-only status of an ``owner_token`` PIN (the setup wizard polls it)."""

    authorized: bool
    expires_at: datetime
    account_name: str | None = None


@dataclass(frozen=True, slots=True)
class StartedQuickConnect:
    """What the client shows the user so they can approve it in a Jellyfin client."""

    handle: str
    code: str
    expires_at: datetime


class PlexPinFlow:
    """Creating, polling and spending Plex PINs (docs/auth.md, sections 4 and 5)."""

    def __init__(
        self,
        sign_in: SignInService,
        handles: HandleRegistry,
        plex_tv: PlexTv,
        server_state: ServerStateRepository,
        install_id: str,
    ) -> None:
        self._sign_in = sign_in
        self._handles = handles
        self._plex_tv = plex_tv
        self._server_state = server_state
        self._install_id = install_id

    async def create(self, purpose: HandlePurpose, binding: Binding, caller: Caller) -> StartedPin:
        """Create a PIN on plex.tv and return the handle bound to this caller.

        An ``owner_token`` PIN works before any media server is configured — it is how
        the wizard obtains one — and is the only purpose that uses the install's own
        client identifier, so revoking a sign-in device never touches the owner token.
        """
        if purpose != "owner_token":
            await self._require_plex(caller)
        client_id = self._install_id if purpose == "owner_token" else str(uuid4())
        name = await self._sign_in.server_name()
        pin = await self._plex_tv.create_pin(client_id, f"Tindeerr ({name})")
        handle = self._handles.create(
            "plex_pin",
            purpose,
            binding,
            client_key=caller.client_key,
            upstream_expiry=pin.expires_at,
            pin=pin,
        )
        return StartedPin(handle.id, self._plex_tv.auth_url(pin), handle.expires_at)

    async def _require_plex(self, caller: Caller) -> None:
        await self._sign_in.require_open(caller)
        settings = await self._sign_in.configured()
        if settings.kind != "plex":
            raise errors.sign_in_method_unavailable("This server does not sign in with Plex.")

    async def status(self, handle_id: str, session_id: str) -> PinStatus:
        """Report whether an ``owner_token`` PIN was approved, without spending it."""
        handle = self._handles.use(
            handle_id, "plex_pin", "owner_token", Binding.session(session_id)
        )
        if handle.token is None:
            try:
                self._handles.throttle(handle)
            except PendingError:
                # Asked again too soon: the last known answer is still "not yet".
                return PinStatus(authorized=False, expires_at=handle.expires_at)
            await self._collect_owner_token(handle_id, session_id)
            handle = self._handles.use(
                handle_id, "plex_pin", "owner_token", Binding.session(session_id)
            )
        return PinStatus(
            authorized=handle.token is not None,
            expires_at=handle.expires_at,
            account_name=handle.account_name,
        )

    async def _collect_owner_token(self, handle_id: str, session_id: str) -> None:
        handle = self._handles.use(
            handle_id, "plex_pin", "owner_token", Binding.session(session_id)
        )
        if handle.pin is None:  # pragma: no cover - every PIN handle carries its PIN
            raise errors.pin_expired()
        token = await self._plex_tv.check_pin(handle.pin)
        if token is None:
            return
        # The token is kept in memory only. It is deliberately **not** registered for
        # log redaction: nothing ever puts it in a log field, and the redaction set is
        # scanned for every line, so request-scoped values must not accumulate in it.
        # The settings store registers the owner token once the connector saves it.
        account = await self._plex_tv.account(token)
        handle.token, handle.account_name = token, account.name
        logger.info("a Plex owner token was approved", extra={"account": account.name})

    async def collect(self, handle_id: str, purpose: HandlePurpose, binding: Binding) -> MediaUser:
        """Poll a ``sign_in`` or ``reauth`` PIN and return the account once approved.

        Raises ``PendingError`` (``202``) while the user has not approved it,
        ``not_a_server_user`` when the account cannot reach the configured server, and
        spends the handle as soon as an answer was obtained.
        """
        handle = self._handles.use(handle_id, "plex_pin", purpose, binding)
        self._handles.throttle(handle)
        if handle.pin is None:  # pragma: no cover - every PIN handle carries its PIN
            raise errors.pin_expired()
        machine_id = await self._machine_id()
        token = await self._plex_tv.check_pin(handle.pin)
        if token is None:
            raise PendingError
        self._handles.spend(handle)
        return await self._account_for(token, handle.pin.client_id, machine_id)

    async def _machine_id(self) -> str:
        """Return the ``machineIdentifier`` the configured Plex server answered with."""
        # Reading it also re-checks that the address still holds that very server.
        await self._sign_in.adapter()
        state = await self._server_state.read()
        machine_id = ServerIdentity.server_id_of(state.media_server_identity, "plex")
        if machine_id is None:
            raise errors.sign_in_method_unavailable("This server does not sign in with Plex.")
        return machine_id

    async def _account_for(self, token: str, client_id: str, machine_id: str) -> MediaUser:
        account = await self._plex_tv.account(token)
        resource = find_server(await self._plex_tv.resources(token), machine_id)
        # The device goes whatever the answer is: Tindeerr keeps no Plex sign-in token,
        # and leaves none live in the user's plex.tv account either.
        await self._plex_tv.delete_device(token, client_id)
        if resource is None:
            raise errors.not_a_server_user()
        return as_media_user(account, admin=resource.owned)


class QuickConnectFlow:
    """Starting, polling and sweeping Jellyfin Quick Connect requests."""

    def __init__(self, sign_in: SignInService, handles: HandleRegistry) -> None:
        self._sign_in = sign_in
        self._handles = handles

    async def create(
        self, purpose: HandlePurpose, binding: Binding, caller: Caller
    ) -> StartedQuickConnect:
        """Ask Jellyfin for a code and bind it to this caller."""
        await self._sign_in.require_open(caller)
        settings = await self._sign_in.configured()
        if settings.kind != "jellyfin":
            raise errors.quick_connect_unavailable()
        adapter = await self._sign_in.adapter()
        try:
            started = await adapter.quick_connect_start()
        except ProblemError:
            # Switched off, or the server no longer offers it: stop advertising it.
            self._sign_in.quick_connect.remember(enabled=None)
            raise
        self._sign_in.quick_connect.remember(enabled=True)
        handle = self._handles.create(
            "quick_connect",
            purpose,
            binding,
            client_key=caller.client_key,
            upstream_expiry=started.expires_at,
            secret=started.secret,
        )
        return StartedQuickConnect(handle.id, started.code, handle.expires_at)

    async def collect(self, handle_id: str, purpose: HandlePurpose, binding: Binding) -> MediaUser:
        """Poll the request and return the approving user, spending the handle."""
        handle = self._handles.use(handle_id, "quick_connect", purpose, binding)
        self._handles.throttle(handle)
        adapter = await self._sign_in.adapter()
        user = await adapter.quick_connect_poll(handle.secret or "")
        if user is None:
            raise PendingError
        # The Jellyfin session the approval created was ended by the poll itself.
        handle.collected = True
        self._handles.spend(handle)
        return user

    def forget_availability(self) -> None:
        """Stop advertising Quick Connect until a probe succeeds again."""
        self._sign_in.quick_connect.remember(enabled=None)

    async def refresh_availability(self) -> None:
        """Re-read ``GET /QuickConnect/Enabled`` so ``server/info`` can stay honest."""
        settings = await self._sign_in.configured()
        if settings.kind != "jellyfin":
            self._sign_in.quick_connect.remember(enabled=False)
            return
        adapter = await self._sign_in.adapter()
        self._sign_in.quick_connect.remember(enabled=await adapter.quick_connect_enabled())

    async def sweep(self) -> int:
        """Collect Quick Connect approvals nobody came back for, and end their sessions.

        Jellyfin creates the session at approval, so an abandoned handle would otherwise
        leave a signed-in "Tindeerr server" device behind. Collecting it once logs it
        out; a server that is unreachable simply keeps it until the next sign-in with
        the same ``DeviceId`` revokes it.
        """
        abandoned = self._handles.sweep()
        if not abandoned:
            return 0
        try:
            adapter = await self._sign_in.adapter()
        except ProblemError as problem:
            # The handles are gone either way; the leftover Jellyfin sessions are
            # revoked at those users' next sign-in, which reuses the same ``DeviceId``.
            logger.warning(
                "could not clean up abandoned Quick Connect approvals",
                extra={"count": len(abandoned), "problem": problem.code},
            )
            return 0
        closed = 0
        for handle in abandoned:
            try:
                if await adapter.quick_connect_poll(handle.secret or "") is not None:
                    closed += 1
            except ProblemError as problem:
                # One dead handle must not stop the others.
                logger.warning(
                    "could not clean up an abandoned Quick Connect approval",
                    extra={"problem": problem.code},
                )
        if closed:
            logger.info("ended abandoned Quick Connect sessions", extra={"count": closed})
        return closed
