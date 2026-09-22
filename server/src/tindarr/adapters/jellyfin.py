"""Jellyfin: the Media Browser API plus Quick Connect (docs/auth.md, section 4).

Everything Jellyfin shares with Emby is in ``tindarr.adapters.mediabrowser``. What is
here is what only Jellyfin has or does differently:

- **10.10 is the floor.** 10.10, 10.11 and 12.x are the tested versions; older ones are
  refused by the connection test (``media_server_unsupported``), because this adapter
  relies on behaviour they do not all have.
- **Quick Connect.** ``POST /QuickConnect/Initiate`` has been a ``POST`` since 10.9 and
  the ``GET`` form is gone in 12.x, so only the ``POST`` is used. ``401`` there means an
  administrator switched Quick Connect off; ``404`` on ``/QuickConnect/Connect`` means
  the code is unknown or has expired.
- **Approval creates the session, not collection.** Jellyfin opens the user's session
  when they approve the code, so a request nobody collects leaves a live session behind.
  Collecting is therefore also the cleanup: ``quick_connect_poll`` authenticates and
  immediately logs that session out, and the handle sweep calls it once more for
  approvals nobody came back for (``tindarr.auth.handles``).
"""

from http import HTTPStatus
from typing import Final

import httpx2

from tindarr.adapters.http import RemoteCallError, read_json, read_mapping
from tindarr.adapters.mediabrowser import MediaBrowserServer
from tindarr.ports import problems
from tindarr.ports.media_server import MediaServerKind, MediaUser, QuickConnectStart

#: Oldest Jellyfin this adapter is tested against (docs/auth.md, section 4).
MINIMUM_VERSION: Final = (10, 10)


class JellyfinServer(MediaBrowserServer):
    """The ``MediaServer`` port for Jellyfin."""

    kind: MediaServerKind = "jellyfin"
    minimum_version: tuple[int, ...] = MINIMUM_VERSION

    async def quick_connect_enabled(self) -> bool:
        """Read ``GET /QuickConnect/Enabled``; ``404`` (older or disabled) reads as off."""
        try:
            async with self._session() as session:
                response = await session.request("GET", "/QuickConnect/Enabled")
                if response.status_code == HTTPStatus.NOT_FOUND:
                    return False
                return read_json(self._expect_ok(response)) is True
        except RemoteCallError as failure:
            raise self._unreachable("quick_connect_enabled", failure) from None

    async def quick_connect_start(self) -> QuickConnectStart:
        """Ask Jellyfin for a code and the secret that polls it."""
        try:
            async with self._session() as session:
                response = await session.request("POST", "/QuickConnect/Initiate")
                if response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND):
                    raise problems.quick_connect_unavailable()
                payload = read_mapping(self._expect_ok(response))
        except RemoteCallError as failure:
            raise self._unreachable("quick_connect_start", failure) from None
        code, secret = payload.get("Code"), payload.get("Secret")
        if not isinstance(code, str) or not isinstance(secret, str) or not code or not secret:
            raise self._unreachable("quick_connect_start", RemoteCallError("no_secret"))
        return QuickConnectStart(code=code, secret=secret)

    @staticmethod
    def _raise_for_collect(response: httpx2.Response) -> None:
        """Map the answers of ``AuthenticateWithQuickConnect``.

        The code was approved a moment ago, so a refusal is not a wrong credential: it
        is the request going stale between the two calls, except ``403``, which is the
        account itself (disabled, too many sessions, device restriction).
        """
        if response.status_code == HTTPStatus.FORBIDDEN:
            raise problems.account_disabled()
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise problems.quick_connect_expired()

    async def quick_connect_poll(self, secret: str) -> MediaUser | None:
        """Return the approving user, or ``None`` while the code is still waiting.

        Once approved, this authenticates **and ends** the Jellyfin session the approval
        created, so nothing is left behind whether the caller keeps the result or not.
        """
        try:
            async with self._session() as session:
                # The secret travels in the query string Jellyfin defines; the HTTP
                # client's own loggers stay at WARNING so no URL is ever logged.
                response = await session.request(
                    "GET", "/QuickConnect/Connect", params={"secret": secret}
                )
                if response.status_code == HTTPStatus.NOT_FOUND:
                    # Jellyfin forgot the code: nothing can revive it.
                    raise problems.quick_connect_expired()
                state = read_mapping(self._expect_ok(response))
                if state.get("Authenticated") is not True:
                    return None
                authenticated = await session.request(
                    "POST",
                    "/Users/AuthenticateWithQuickConnect",
                    json_body={"Secret": secret},
                )
                self._raise_for_collect(authenticated)
                return await self._collect_sign_in(session, read_mapping(authenticated))
        except RemoteCallError as failure:
            raise self._unreachable("quick_connect_poll", failure) from None
