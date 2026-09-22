"""What Jellyfin and Emby really share: the Media Browser HTTP API.

Both descend from Emby 3, so the endpoints this step needs are the same calls with the
same bodies. What differs is small and explicit in the two subclasses
(``tindarr.adapters.jellyfin``, ``tindarr.adapters.emby``): the product name in
``/System/Info/Public``, the minimum version, and Quick Connect, which only Jellyfin
has. Nothing else is parameterised, so neither server's behaviour is hidden behind a
setting nobody reads.

**The client identity** (docs/auth.md, section 4) is one header on every call::

    Authorization: MediaBrowser Client="Tindarr", Device="Tindarr server",
      DeviceId="<install id>", Version="1", Token="<token>"

- It is the only form used. Jellyfin 12 ignores ``X-Emby-Token``,
  ``X-Emby-Authorization``, the ``Emby`` scheme and ``?api_key=``.
- ``Version`` is a **constant**, not the Tindarr version: Emby identifies a device by
  all four values, so a version that moves would register a new device at every release.
- ``DeviceId`` is the install id, stable for the life of the database. Jellyfin revokes
  a user's older tokens with the same ``DeviceId`` at each sign-in, which also clears any
  session an earlier crash left behind.
- ``Token`` is the admin API key for admin calls and the user's own fresh token for the
  logout that ends the session a sign-in opened; it is absent from the sign-in calls
  themselves.

Every failure that is not a documented sign-in answer becomes
``media_server_unreachable``: the caller cannot act on the difference between "the
server is down" and "the server answered something we cannot read", and the contract's
sign-in operations only document ``503`` for both.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus
from itertools import takewhile
from typing import Any, Final

import httpx2

from tindarr.adapters.http import (
    NO_RESPONSE_REASONS,
    HttpSession,
    RemoteCallError,
    as_flag,
    as_object,
    as_text,
    read_list,
    read_mapping,
)
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
    normalize_user_id,
)

#: The four values that make up the client identity, as Emby and Jellyfin record it.
CLIENT_NAME: Final = "Tindarr"
DEVICE_NAME: Final = "Tindarr server"
CLIENT_VERSION: Final = "1"
#: Used when the connector has no install id yet (only reachable in tests).
_FALLBACK_DEVICE_ID: Final = "tindarr"
#: The one endpoint both products gate on the administrator role (see ``_check_elevation``).
ELEVATION_PROBE_PATH: Final = "/System/Configuration"

logger = logging.getLogger(__name__)


def authorization_header(device_id: str, token: str | None = None) -> str:
    """Build the ``Authorization: MediaBrowser …`` value, with the token when there is one."""
    parts = [
        f'Client="{CLIENT_NAME}"',
        f'Device="{DEVICE_NAME}"',
        f'DeviceId="{device_id or _FALLBACK_DEVICE_ID}"',
        f'Version="{CLIENT_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


def parse_version(value: str | None) -> tuple[int, ...]:
    """Return a dotted version as a tuple of numbers, ``()`` when it is not one."""
    if not value:
        return ()
    parts: list[int] = []
    for item in value.split("."):
        # Leading digits only: "0-rc1" is release candidate 1 of 0, not version 1.
        digits = "".join(takewhile(str.isdigit, item))
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


class MediaBrowserServer:
    """The step-2 ``MediaServer`` operations shared by Jellyfin and Emby."""

    kind: MediaServerKind
    #: Oldest version supported, or ``()`` when no floor is enforced.
    minimum_version: tuple[int, ...] = ()

    def __init__(
        self,
        connection: MediaServerConnection,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._connection = connection
        self._transport = transport

    # --- plumbing -------------------------------------------------------------------

    def _session(self, token: str | None = None) -> HttpSession:
        return HttpSession(
            self._connection.url,
            headers={"Authorization": authorization_header(self._connection.device_id, token)},
            verify_tls=self._connection.verify_tls,
            transport=self._transport,
        )

    def _api_key_session(self) -> HttpSession:
        return self._session(self._connection.secret)

    @staticmethod
    def _expect_ok(response: httpx2.Response) -> httpx2.Response:
        if response.status_code != HTTPStatus.OK:
            raise RemoteCallError(f"status_{response.status_code}")
        return response

    # --- identity and health --------------------------------------------------------

    async def _public_info(self, session: HttpSession) -> Mapping[str, Any]:
        response = await session.request("GET", "/System/Info/Public")
        return read_mapping(self._expect_ok(response))

    def _identity_of(self, info: Mapping[str, Any]) -> ServerIdentity:
        server_id = normalize_server_id(as_text(info.get("Id")) or "")
        if server_id is None:
            raise RemoteCallError("no_server_id")
        return ServerIdentity(
            kind=self._product_kind(info),
            server_id=server_id,
            name=as_text(info.get("ServerName")),
            version=as_text(info.get("Version")),
        )

    def _product_kind(self, info: Mapping[str, Any]) -> MediaServerKind:
        """Which product answered, read from ``ProductName``.

        Jellyfin 10.10 and later always send it; some Emby builds do not, and an absent
        value is therefore read as "the declared kind". A value that names the *other*
        product is what this catches: the connector then refuses to save
        (``media_server_unsupported``).
        """
        product = (as_text(info.get("ProductName")) or "").lower()
        if "jellyfin" in product:
            return "jellyfin"
        if "emby" in product:
            return "emby"
        return self.kind

    def _version_supported(self, info: Mapping[str, Any]) -> bool:
        if not self.minimum_version:
            return True
        version = parse_version(as_text(info.get("Version")))
        return bool(version) and version >= self.minimum_version

    async def identify(self) -> ServerIdentity:
        """Read ``GET /System/Info/Public``: server id, product and version."""
        try:
            async with self._session() as session:
                return self._identity_of(await self._public_info(session))
        except RemoteCallError as failure:
            raise self._unreachable("identify", failure) from None

    async def test(self) -> ConnectionCheck:
        """Check the address, the product, the version and the API key.

        Never raises for a remote failure: the result is one of the coarse health values
        the contract documents, with no response body (the security model, section 7).
        """
        try:
            async with self._session() as session:
                info = await self._public_info(session)
        except RemoteCallError as failure:
            return ConnectionCheck(self._health_of(failure))
        name, version = as_text(info.get("ServerName")), as_text(info.get("Version"))
        if self._product_kind(info) != self.kind or not self._version_supported(info):
            # Both mean "that address answers, but not as the declared product or a
            # version Tindarr supports": one problem code, ``media_server_unsupported``.
            return ConnectionCheck("unsupported_version", name, version)
        return ConnectionCheck(await self._check_api_key(), name, version)

    async def _check_api_key(self) -> ConnectorHealth:
        try:
            async with self._api_key_session() as session:
                response = await session.request("GET", "/Users")
                if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    return "unauthorized"
                read_list(self._expect_ok(response))
                return await self._check_elevation(session)
        except RemoteCallError as failure:
            return self._health_of(failure)

    async def _check_elevation(self, session: HttpSession) -> ConnectorHealth:
        """Check that the key is an administrator's, not just a valid user token.

        ``GET /Users`` is **not** the proof it looks like: Jellyfin and Emby answer
        ``200`` to any authenticated caller and silently filter the list to what that
        caller may see. A user access token pasted into the API key field would pass,
        and the hourly sync would then read that one-entry list as "everyone else was
        removed" (docs/auth.md, section 6).

        ``GET /System/Configuration`` is the elevation-gated endpoint both products
        have: Jellyfin's ``ConfigurationController`` requires the ``RequiresElevation``
        policy, Emby's requires the ``Admin`` role. A ``401`` or ``403`` there is
        therefore a key that works but is not an administrator's.

        A server that answers something else — an older or forked build without that
        route — leaves the question open. Refusing would lock such an operator out of
        their own setup over a probe, so the key is accepted and the doubt is logged;
        the sync's own guard (a list with no users changes nothing) stays the backstop.
        """
        response = await session.request("GET", ELEVATION_PROBE_PATH)
        if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return "unauthorized"
        if response.status_code != HTTPStatus.OK:
            logger.warning(
                "could not check that the media server API key is an administrator key",
                extra=self._log() | {"status": response.status_code},
            )
        return "ok"

    @staticmethod
    def _health_of(failure: RemoteCallError) -> ConnectorHealth:
        if failure.reason in NO_RESPONSE_REASONS:
            return "unreachable"
        return "unexpected_response"

    # --- sign-in --------------------------------------------------------------------

    async def authenticate_password(self, username: str, password: str) -> MediaUser:
        """Check a password with ``POST /Users/AuthenticateByName`` and end the session.

        The call carries no token: it is the one that creates one. Its answer is mapped
        to the contract's sign-in problems, and nothing about the credentials is kept.
        """
        try:
            async with self._session() as session:
                response = await session.request(
                    "POST",
                    "/Users/AuthenticateByName",
                    json_body={"Username": username, "Pw": password},
                )
                self._raise_for_sign_in(response)
                return await self._collect_sign_in(session, read_mapping(response))
        except RemoteCallError as failure:
            raise self._unreachable("authenticate_password", failure) from None

    @staticmethod
    def _raise_for_sign_in(response: httpx2.Response) -> None:
        """Turn the documented sign-in answers into their problems, in one place."""
        status = response.status_code
        if status == HTTPStatus.UNAUTHORIZED:
            # Unknown user and wrong password are the same answer, by design. Only
            # ``401`` counts: a ``400`` is a request the server did not understand, and
            # must not spend the per-username budget that protects its lockout counter.
            raise problems.invalid_credentials()
        if status == HTTPStatus.FORBIDDEN:
            # Disabled on the media server, too many active sessions, or a user limited
            # to devices that do not include ours.
            raise problems.account_disabled()

    async def _collect_sign_in(self, session: HttpSession, payload: Mapping[str, Any]) -> MediaUser:
        """Read the user from an authentication response, then end the session it opened."""
        user = as_object(payload.get("User"))
        if user is None:
            raise RemoteCallError("no_user")
        media_user = self._media_user(user)
        await self._logout(session, as_text(payload.get("AccessToken")))
        return media_user

    async def _logout(self, session: HttpSession, token: str | None) -> None:
        """End the media server session the sign-in opened; a failure is only logged."""
        if not token:
            return
        header = authorization_header(self._connection.device_id, token)
        try:
            response = await session.request(
                "POST", "/Sessions/Logout", headers={"Authorization": header}
            )
        except RemoteCallError as failure:
            logger.warning(
                "could not end the media server session opened for a sign-in",
                extra={"media_server": self.kind, "reason": failure.reason},
            )
            return
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            logger.warning(
                "the media server refused to end the session opened for a sign-in",
                extra={"media_server": self.kind, "status": response.status_code},
            )

    def _media_user(self, payload: Mapping[str, Any]) -> MediaUser:
        policy: Mapping[str, Any] = as_object(payload.get("Policy")) or {}
        raw_id = as_text(payload.get("Id")) or ""
        try:
            user_id = normalize_user_id(self.kind, raw_id)
        except ValueError:
            raise RemoteCallError("bad_user_id") from None
        return MediaUser(
            id=user_id,
            name=as_text(payload.get("Name")) or user_id,
            is_admin=as_flag(policy.get("IsAdministrator"), default=False),
            remote_access=as_flag(policy.get("EnableRemoteAccess"), default=False),
            disabled=as_flag(policy.get("IsDisabled"), default=False),
        )

    # --- the hourly sync ------------------------------------------------------------

    async def list_users(self) -> list[MediaUser]:
        """Return every user of the server (``GET /Users`` with the admin API key)."""
        try:
            async with self._api_key_session() as session:
                response = await session.request("GET", "/Users")
                rows = read_list(self._expect_ok(response))
        except RemoteCallError as failure:
            raise self._unreachable("list_users", failure) from None
        return [
            user
            for row in rows
            if (payload := as_object(row)) is not None
            and (user := self._maybe_user(payload)) is not None
        ]

    def _maybe_user(self, row: Mapping[str, Any]) -> MediaUser | None:
        try:
            return self._media_user(row)
        except RemoteCallError:
            # One unreadable row must not stop the sync for everyone else.
            logger.warning("skipped a media server user Tindarr cannot read", extra=self._log())
            return None

    # --- Quick Connect (Jellyfin only) ----------------------------------------------

    async def quick_connect_enabled(self) -> bool:
        """Whether Quick Connect can be used here. Emby has none, so this is ``False``."""
        return False

    async def quick_connect_start(self) -> QuickConnectStart:
        """Refuse: this server has no Quick Connect."""
        raise problems.quick_connect_unavailable()

    async def quick_connect_poll(self, secret: str) -> MediaUser | None:
        """Refuse: this server has no Quick Connect."""
        raise problems.quick_connect_unavailable()

    # --- failures -------------------------------------------------------------------

    def _log(self) -> dict[str, str]:
        return {"media_server": self.kind}

    def _unreachable(self, operation: str, failure: RemoteCallError) -> Exception:
        logger.warning(
            "the media server did not answer usably",
            extra=self._log() | {"operation": operation, "reason": failure.reason},
        )
        return problems.media_server_unreachable()
