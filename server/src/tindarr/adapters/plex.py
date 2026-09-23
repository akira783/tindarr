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
from typing import Any, Final, cast
from urllib.parse import quote

import httpx2

from tindarr.adapters.http import (
    HttpSession,
    RemoteCallError,
    as_object,
    as_text,
    read_mapping,
)
from tindarr.adapters.plex_library import (
    HISTORY_LIMIT,
    MAX_PAGES,
    PAGE_SIZE,
    WatchHistory,
    library_item,
    owner_engagement,
)
from tindarr.core.clock import Clock, SystemClock
from tindarr.core.errors import ProblemError, RateLimitedError
from tindarr.ports import problems
from tindarr.ports.media_server import (
    ConnectionCheck,
    ConnectorHealth,
    Engagement,
    LibraryIndex,
    LibraryItem,
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
#: The libraries, and everything in one of them.
_SECTIONS_PATH: Final = "/library/sections"
#: Every viewing the server recorded, filterable by account for its owner.
_HISTORY_PATH: Final = "/status/sessions/history/all"
#: Where Plex's own web client opens an item, by server and metadata key.
_APP_URL: Final = "https://app.plex.tv/desktop/#!/server/{machine_id}/details?key={key}"

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
        clock: Clock | None = None,
    ) -> None:
        self._connection = connection
        self._plex_tv = plex_tv
        self._transport = transport
        self._clock = clock or SystemClock()
        #: Read from ``/identity``; a deep link cannot be built before it is known.
        self._machine_id: str | None = None

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
                response = await session.request_bounded("GET", _IDENTITY_PATH)
                container = self._container(read_mapping(_expect_ok(response)))
        except RemoteCallError as failure:
            raise self._unreachable("identify", failure) from None
        machine_id = normalize_server_id(as_text(container.get("machineIdentifier")) or "")
        if machine_id is None:
            raise self._unreachable("identify", RemoteCallError("no_machine_identifier"))
        self._machine_id = machine_id
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
                response = await session.request_bounded("GET", "/")
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

    # --- the library and one user's history (step 3) --------------------------------

    async def library_ids(self) -> LibraryIndex:
        """List every film and series in every library, with their TMDb ids."""
        await self.identify()
        try:
            async with self._session(token=self._connection.secret) as session:
                items = [
                    item
                    for key in await self._section_keys(session)
                    for row in await self._section_rows(session, key)
                    if (item := library_item(row)) is not None
                ]
        except RemoteCallError as failure:
            raise self._unreachable("library_ids", failure) from None
        logger.debug("read the Plex library", extra={"items": len(items)})
        return LibraryIndex(items)

    async def engagement(self, user: MediaUser) -> list[Engagement]:
        """Read what one user watched — fully for the owner, from the history otherwise.

        The stored credential is the owner's account token, and a Plex server serves the
        view state of whoever asked. So the owner's own progress is read straight from
        the library, while every other account is reconstructed from the server's
        history, which the owner may filter by account but which records completed
        viewings only (see the module docstring and ADR 0012).
        """
        token, client_id = self._connection.secret, self._connection.device_id
        # Also what fills the machine identifier a deep link needs, so an engagement
        # read leaves ``deep_link`` usable exactly as a library read does.
        await self.identify()
        try:
            owner = as_media_user(await self._plex_tv.account(token, client_id), admin=True)
        except RateLimitedError:
            raise problems.plex_tv_unreachable() from None
        try:
            async with self._session(token=token) as session:
                keys = await self._section_keys(session)
                library = [row for key in keys for row in await self._section_rows(session, key)]
                if user.id == owner.id:
                    return owner_engagement(library, self._clock.now())
                history = await self._history(session, user.id)
        except RemoteCallError as failure:
            raise self._unreachable("engagement", failure) from None
        return WatchHistory(history).engagements(library, self._clock.now())

    async def _section_keys(self, session: HttpSession) -> list[str]:
        response = await session.request_bounded("GET", _SECTIONS_PATH)
        container = self._container(read_mapping(_expect_ok(response)))
        directories = container.get("Directory")
        if not isinstance(directories, list):
            return []
        keys: list[str] = []
        for value in cast("list[object]", directories):
            entry = as_object(value)
            key = as_text(entry.get("key")) if entry is not None else None
            kind = as_text(entry.get("type")) if entry is not None else None
            if key is not None and kind in ("movie", "show"):
                keys.append(key)
        return keys

    async def _section_rows(
        self, session: HttpSession, section_key: str
    ) -> list[Mapping[str, Any]]:
        return await self._paged(
            session, f"{_SECTIONS_PATH}/{section_key}/all", {"includeGuids": "1"}, MAX_PAGES
        )

    async def _history(self, session: HttpSession, account_id: str) -> list[Mapping[str, Any]]:
        return await self._paged(
            session,
            _HISTORY_PATH,
            {"accountID": account_id, "sort": "viewedAt:desc"},
            max(HISTORY_LIMIT // PAGE_SIZE, 1),
        )

    async def _paged(
        self, session: HttpSession, path: str, params: Mapping[str, str], max_pages: int
    ) -> list[Mapping[str, Any]]:
        """Read a ``MediaContainer`` listing page by page, through Plex's own container range.

        Plex answers an out-of-range start with an empty container rather than an error,
        so a short page — or the page cap — ends the loop.
        """
        collected: list[Mapping[str, Any]] = []
        for page in range(max_pages):
            response = await session.request_bounded(
                "GET",
                path,
                params=dict(params)
                | {
                    "X-Plex-Container-Start": str(page * PAGE_SIZE),
                    "X-Plex-Container-Size": str(PAGE_SIZE),
                },
            )
            container = self._container(read_mapping(_expect_ok(response)))
            rows = container.get("Metadata")
            if not isinstance(rows, list):
                return collected
            found = [row for value in cast("list[object]", rows) if (row := as_object(value))]
            collected.extend(found)
            if len(found) < PAGE_SIZE:
                return collected
        logger.warning("stopped reading a Plex listing at the page cap", extra={"path": path})
        return collected

    def deep_link(self, item: LibraryItem) -> str | None:
        """Return the app.plex.tv URL for an item, once the server's identifier is known.

        Plex addresses an item by server **and** metadata key, so the link cannot be
        built before ``identify`` has run. ``library_ids`` and ``engagement`` both run
        it, which is how every item this could be asked about was obtained; an adapter
        that has read neither answers ``None`` rather than guessing an identifier.
        """
        if self._machine_id is None:
            return None
        key = quote(f"/library/metadata/{item.item_id}", safe="")
        return _APP_URL.format(machine_id=self._machine_id, key=key)

    # --- failures -------------------------------------------------------------------

    @staticmethod
    def _unreachable(operation: str, failure: RemoteCallError) -> Exception:
        logger.warning(
            "the Plex server did not answer usably",
            extra={"media_server": "plex", "operation": operation, "reason": failure.reason},
        )
        return problems.media_server_unreachable()
