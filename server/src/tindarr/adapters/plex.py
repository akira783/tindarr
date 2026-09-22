"""Plex Media Server: identity, connection test and users (docs/auth.md, sections 4, 6).

Plex splits in two. The **server** answers at the configured address and is only asked
who it is (``GET /identity``) and whether the owner token works there. Everything about
*people* — who approved a PIN, which servers an account may reach, who the library is
shared with — comes from **plex.tv** (``tindarr.adapters.plextv``), because a Plex
server does not list its users.

Consequences this adapter encodes:

- The configured server is matched by ``machineIdentifier`` only, read from the server's
  own ``/identity``. plex.tv resource names and advertised URLs are self-reported, so
  they decide nothing.
- The owner token must come from the account that **owns** that resource, otherwise the
  connector is refused (``plex_owner_required``): a friend's token would otherwise let
  anyone with an account pass as the administrator.
- There is no password and no Quick Connect here: both refuse with the contract's
  ``sign_in_method_unavailable`` and ``quick_connect_unavailable``.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, Final

import httpx2

from tindarr.adapters.http import (
    HttpSession,
    RemoteCallError,
    as_object,
    as_text,
    read_mapping,
)
from tindarr.core.errors import ProblemError, RateLimitedError
from tindarr.ports import problems
from tindarr.ports.media_server import (
    ConnectionCheck,
    ConnectorHealth,
    MediaServerConnection,
    MediaServerKind,
    MediaUser,
    QuickConnectStart,
    ServerIdentity,
    normalize_server_id,
)
from tindarr.ports.plextv import PlexResource, PlexTv, as_media_user, find_server

#: What the server answers its own ``machineIdentifier`` and version on.
_IDENTITY_PATH: Final = "/identity"

logger = logging.getLogger(__name__)


def _expect_ok(response: httpx2.Response) -> httpx2.Response:
    """Return the response, or fail when the Plex server answered something else."""
    if response.status_code != HTTPStatus.OK:
        raise RemoteCallError(f"status_{response.status_code}")
    return response


class PlexServer:
    """The ``MediaServer`` port for Plex, over the server itself and plex.tv."""

    kind: MediaServerKind = "plex"

    def __init__(
        self,
        connection: MediaServerConnection,
        plex_tv: PlexTv,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._connection = connection
        self._plex_tv = plex_tv
        self._transport = transport

    def _session(self, *, token: str | None = None) -> HttpSession:
        headers = {"Accept": "application/json"}
        if token:
            # In the header, never in a query string (docs/auth.md, section 4).
            headers["X-Plex-Token"] = token
        return HttpSession(
            self._connection.url,
            headers=headers,
            verify_tls=self._connection.verify_tls,
            transport=self._transport,
        )

    # --- identity -------------------------------------------------------------------

    async def identify(self) -> ServerIdentity:
        """Read the server's own ``machineIdentifier`` from ``GET /identity``."""
        try:
            async with self._session() as session:
                response = await session.request("GET", _IDENTITY_PATH)
                container = self._container(read_mapping(_expect_ok(response)))
        except RemoteCallError as failure:
            raise self._unreachable("identify", failure) from None
        machine_id = normalize_server_id(as_text(container.get("machineIdentifier")) or "")
        if machine_id is None:
            raise self._unreachable("identify", RemoteCallError("no_machine_identifier"))
        return ServerIdentity(
            kind="plex",
            server_id=machine_id,
            name=as_text(container.get("friendlyName")),
            version=as_text(container.get("version")),
        )

    @staticmethod
    def _container(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Plex wraps its JSON in ``MediaContainer``; some builds answer flat."""
        return as_object(payload.get("MediaContainer")) or payload

    # --- connection test ------------------------------------------------------------

    async def test(self) -> ConnectionCheck:
        """Check the address, that the token owns this server, and that it works there.

        Only ``plex_owner_required`` escapes: it is not a remote failure but a refusal
        the administrator must see and act on (docs/auth.md, section 6).
        """
        try:
            identity = await self.identify()
        except ProblemError:
            return ConnectionCheck("unreachable")
        try:
            await self._owned_resource(self._connection.secret, identity.server_id)
        except ProblemError as problem:
            if problem.code == "plex_owner_required":
                raise
            return ConnectionCheck("unreachable", identity.name, identity.version)
        return ConnectionCheck(await self._check_server_token(), identity.name, identity.version)

    async def _check_server_token(self) -> ConnectorHealth:
        try:
            async with self._session(token=self._connection.secret) as session:
                response = await session.request("GET", "/")
        except RemoteCallError:
            return "unreachable"
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return "unauthorized"
        return "ok" if response.status_code == HTTPStatus.OK else "unexpected_response"

    async def _owned_resource(self, token: str, machine_id: str) -> PlexResource:
        """Return the configured server among the token's resources, if it owns it."""
        found = await self._plex_tv.resources(token, self._connection.device_id)
        resource = find_server(found, machine_id)
        if resource is None or not resource.owned:
            raise problems.plex_owner_required()
        return resource

    # --- sign-in (neither exists on Plex) -------------------------------------------

    async def authenticate_password(self, username: str, password: str) -> MediaUser:
        """Refuse: Plex accounts sign in through a PIN, never through Tindarr."""
        raise problems.sign_in_method_unavailable(
            "This server signs in with a Plex PIN, not with a password."
        )

    async def quick_connect_enabled(self) -> bool:
        """Plex has no Quick Connect."""
        return False

    async def quick_connect_start(self) -> QuickConnectStart:
        """Refuse: Plex has no Quick Connect."""
        raise problems.quick_connect_unavailable()

    async def quick_connect_poll(self, secret: str) -> MediaUser | None:
        """Refuse: Plex has no Quick Connect."""
        raise problems.quick_connect_unavailable()

    # --- the hourly sync ------------------------------------------------------------

    async def list_users(self) -> list[MediaUser]:
        """Return the owner and the accounts this server is shared with.

        Plex has no notion of a disabled user: someone who lost access simply stops
        appearing here, which the sync reads as "removed on the media server".
        """
        token, client_id = self._connection.secret, self._connection.device_id
        identity = await self.identify()
        try:
            owner = await self._plex_tv.account(token, client_id)
            shared = await self._plex_tv.shared_users(token, identity.server_id, client_id)
        except RateLimitedError:
            # The sync runs again in an hour; nothing is changed on a partial read.
            raise problems.plex_tv_unreachable() from None
        users = [as_media_user(owner, admin=True)]
        users.extend(
            as_media_user(account, admin=False) for account in shared if account.id != owner.id
        )
        return users

    # --- failures -------------------------------------------------------------------

    @staticmethod
    def _unreachable(operation: str, failure: RemoteCallError) -> Exception:
        logger.warning(
            "the Plex server did not answer usably",
            extra={"media_server": "plex", "operation": operation, "reason": failure.reason},
        )
        return problems.media_server_unreachable()
