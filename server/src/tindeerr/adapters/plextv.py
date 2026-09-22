"""plex.tv: PINs, accounts, resources, devices and shared users (docs/auth.md, §4).

Only the classic PIN flow is used. Tindeerr never registers a JWK with plex.tv: doing so
with the owner token would expire that token, and the PIN flow is not deprecated.

Every call carries ``X-Plex-Product`` and an ``X-Plex-Client-Identifier``, and tokens
always travel in the ``X-Plex-Token`` **header**, never in a query string, so no
credential can end up in a proxy's access log.

Two endpoints are older than the rest and answer XML: ``/devices.xml``, used to delete
the device a sign-in PIN created, and ``/api/users``, used by the hourly sync. They are
what python-plexapi uses; they are not in Plex's official documentation, which is why
deleting a device is best effort and never fails a sign-in that already succeeded.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, Final
from xml.etree.ElementTree import Element

import httpx2

from tindeerr.adapters.http import (
    HttpSession,
    RemoteCallError,
    as_flag,
    as_object,
    as_text,
    read_list,
    read_mapping,
    read_xml,
)
from tindeerr.core.errors import ProblemError, RateLimitedError
from tindeerr.ports import problems
from tindeerr.ports.plextv import PlexAccount, PlexPin, PlexResource

PLEX_TV_URL: Final = "https://plex.tv"
PRODUCT: Final = "Tindeerr"
#: Where the user approves a PIN. ``context[device][product]`` names Tindeerr on the page.
AUTH_URL_TEMPLATE: Final = (
    "https://app.plex.tv/auth#?clientID={client_id}&code={code}"
    "&context%5Bdevice%5D%5Bproduct%5D=" + PRODUCT
)
#: Used when plex.tv gives neither ``expiresAt`` nor ``expiresIn``; auth caps it anyway.
DEFAULT_PIN_LIFETIME: Final = timedelta(minutes=15)
#: How long to wait when plex.tv answers ``429`` without a usable ``Retry-After``.
DEFAULT_BACKOFF_MS: Final = 5000
_MS: Final = 1000

logger = logging.getLogger(__name__)


class PlexTvClient:
    """The ``PlexTv`` port, talking to the real plex.tv (or a test transport)."""

    def __init__(
        self,
        transport: httpx2.AsyncBaseTransport | None = None,
        base_url: str = PLEX_TV_URL,
    ) -> None:
        self._transport = transport
        self._base_url = base_url

    def _session(self, *, client_id: str = "", token: str | None = None) -> HttpSession:
        headers = {"Accept": "application/json", "X-Plex-Product": PRODUCT}
        if client_id:
            headers["X-Plex-Client-Identifier"] = client_id
        if token:
            headers["X-Plex-Token"] = token
        return HttpSession(self._base_url, headers=headers, transport=self._transport)

    # --- PINs -----------------------------------------------------------------------

    def auth_url(self, pin: PlexPin) -> str:
        """Return the plex.tv page where the user approves ``pin``."""
        return AUTH_URL_TEMPLATE.format(client_id=pin.client_id, code=pin.code)

    async def create_pin(self, client_id: str, device_name: str) -> PlexPin:
        """Create a strong PIN; ``device_name`` is what the approval page shows."""
        payload = await self._json_object(
            "POST",
            "/api/v2/pins",
            client_id=client_id,
            params={"strong": "true"},
            headers={"X-Plex-Device-Name": device_name},
            expected=(HTTPStatus.CREATED, HTTPStatus.OK),
        )
        pin_id, code = as_text(payload.get("id")), as_text(payload.get("code"))
        if pin_id is None or code is None:
            raise self._unusable("create_pin", RemoteCallError("no_pin"))
        return PlexPin(
            id=pin_id, code=code, client_id=client_id, expires_at=self._expiry_of(payload)
        )

    async def check_pin(self, pin: PlexPin) -> str | None:
        """Return the PIN's token once it was approved, else ``None``.

        A PIN plex.tv no longer knows (``404``) is not an error either: the handle's own
        expiry is what the caller reports.
        """
        response = await self._call("GET", f"/api/v2/pins/{pin.id}", client_id=pin.client_id)
        if response.status_code == HTTPStatus.NOT_FOUND:
            return None
        self._expect(response, (HTTPStatus.OK,), "check_pin")
        try:
            return as_text(read_mapping(response).get("authToken"))
        except RemoteCallError as failure:
            raise self._unusable("check_pin", failure) from None

    @staticmethod
    def _expiry_of(payload: Mapping[str, Any]) -> datetime:
        expires_at = as_text(payload.get("expiresAt"))
        if expires_at is not None:
            try:
                parsed = datetime.fromisoformat(expires_at)
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        seconds = payload.get("expiresIn")
        lifetime = (
            timedelta(seconds=int(seconds))
            if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0
            else DEFAULT_PIN_LIFETIME
        )
        return datetime.now(UTC) + lifetime

    # --- account and resources ------------------------------------------------------

    async def account(self, token: str, client_id: str) -> PlexAccount:
        """Return the plex.tv account a token belongs to."""
        payload = await self._json_object("GET", "/api/v2/user", client_id=client_id, token=token)
        account = self._account_of(payload.get("id"), as_text(payload.get("title")), payload)
        if account is None:
            raise self._unusable("account", RemoteCallError("no_account"))
        return account

    @staticmethod
    def _account_of(
        raw_id: object, title: str | None, payload: Mapping[str, Any]
    ) -> PlexAccount | None:
        account_id = as_text(raw_id)
        if account_id is None or not account_id.isdigit():
            return None
        name = title or as_text(payload.get("username")) or account_id
        return PlexAccount(id=account_id, name=name)

    async def resources(self, token: str, client_id: str) -> list[PlexResource]:
        """Return the resources the token may reach, as plex.tv reports them."""
        response = await self._call("GET", "/api/v2/resources", client_id=client_id, token=token)
        self._expect(response, (HTTPStatus.OK,), "resources")
        try:
            rows = read_list(response)
        except RemoteCallError as failure:
            raise self._unusable("resources", failure) from None
        return [
            resource
            for row in rows
            if (payload := as_object(row)) is not None
            and (resource := self._resource_of(payload)) is not None
        ]

    @staticmethod
    def _resource_of(row: Mapping[str, Any]) -> PlexResource | None:
        identifier = as_text(row.get("clientIdentifier"))
        if identifier is None:
            return None
        provides = as_text(row.get("provides")) or ""
        return PlexResource(
            client_identifier=identifier,
            name=as_text(row.get("name")) or identifier,
            owned=as_flag(row.get("owned"), default=False),
            provides=tuple(item.strip() for item in provides.split(",") if item.strip()),
        )

    # --- devices (unofficial, best effort) ------------------------------------------

    async def delete_device(self, token: str, client_id: str) -> bool:
        """Delete the plex.tv device of ``client_id``; ``False`` when it did not work.

        Never raises: the sign-in it follows has already succeeded, and the user can
        always remove the device from their plex.tv "Authorized devices" page.
        """
        try:
            device_id = await self._device_id(token, client_id)
            if device_id is None:
                return False
            response = await self._call(
                "DELETE", f"/devices/{device_id}.xml", client_id=client_id, token=token
            )
            deleted = response.status_code < HTTPStatus.BAD_REQUEST
        except (ProblemError, RemoteCallError) as failure:
            reason = failure.code if isinstance(failure, ProblemError) else failure.reason
            logger.warning(
                "could not remove the plex.tv device created for a sign-in",
                extra={"reason": reason},
            )
            return False
        if not deleted:
            logger.warning(
                "plex.tv refused to remove the device created for a sign-in",
                extra={"status": response.status_code},
            )
        return deleted

    async def _device_id(self, token: str, client_id: str) -> str | None:
        response = await self._call("GET", "/devices.xml", client_id=client_id, token=token)
        if response.status_code != HTTPStatus.OK:
            return None
        for device in read_xml(response).iter("Device"):
            if device.get("clientIdentifier") == client_id:
                return device.get("id")
        return None

    # --- the hourly sync ------------------------------------------------------------

    async def shared_users(
        self, owner_token: str, machine_id: str, client_id: str
    ) -> list[PlexAccount]:
        """Return the accounts this server is shared with (``GET /api/users``, XML)."""
        response = await self._call("GET", "/api/users", client_id=client_id, token=owner_token)
        self._expect(response, (HTTPStatus.OK,), "shared_users")
        try:
            root = read_xml(response)
        except RemoteCallError as failure:
            raise self._unusable("shared_users", failure) from None
        return [
            account
            for user in root.iter("User")
            if self._shares(user, machine_id)
            and (account := self._account_of(user.get("id"), user.get("title"), {})) is not None
        ]

    @staticmethod
    def _shares(user: Element, machine_id: str) -> bool:
        return any(server.get("machineIdentifier") == machine_id for server in user.iter("Server"))

    # --- plumbing -------------------------------------------------------------------

    async def _call(  # noqa: PLR0913 - one per part of a plex.tv call
        self,
        method: str,
        path: str,
        *,
        client_id: str = "",
        token: str | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        try:
            async with self._session(client_id=client_id, token=token) as session:
                response = await session.request(method, path, params=params, headers=headers)
        except RemoteCallError as failure:
            raise self._unusable(method + " " + path.split("/")[1], failure) from None
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            raise RateLimitedError(self._backoff_ms(response), "plex.tv is asking us to slow down.")
        return response

    @staticmethod
    def _backoff_ms(response: httpx2.Response) -> int:
        header = response.headers.get("retry-after", "")
        return int(header) * _MS if header.isdigit() else DEFAULT_BACKOFF_MS

    async def _json_object(  # noqa: PLR0913 - forwarded to ``_call``
        self,
        method: str,
        path: str,
        *,
        client_id: str = "",
        token: str | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        expected: tuple[HTTPStatus, ...] = (HTTPStatus.OK,),
    ) -> Mapping[str, Any]:
        response = await self._call(
            method, path, client_id=client_id, token=token, params=params, headers=headers
        )
        self._expect(response, expected, path)
        try:
            return read_mapping(response)
        except RemoteCallError as failure:
            raise self._unusable(path, failure) from None

    def _expect(
        self, response: httpx2.Response, expected: tuple[HTTPStatus, ...], operation: str
    ) -> None:
        if response.status_code not in expected:
            raise self._unusable(operation, RemoteCallError(f"status_{response.status_code}"))

    @staticmethod
    def _unusable(operation: str, failure: RemoteCallError) -> Exception:
        logger.warning(
            "plex.tv did not answer usably",
            extra={"operation": operation, "reason": failure.reason},
        )
        return problems.plex_tv_unreachable()
