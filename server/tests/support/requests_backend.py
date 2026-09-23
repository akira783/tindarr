"""A fake Seerr (Jellyseerr, Overseerr), behind ``httpx2.MockTransport``.

It keeps what the real backend keeps — users with their media server ids, the requests
it has already accepted, how far each title has got — and records every request with its
headers, so a test can assert that the user listing carried **no** ``X-API-User`` and
that filing one carried exactly the right one.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import parse_qs

import httpx2

#: Where the tests place the backend. The name never resolves: every call is mocked.
SEERR_URL: Final = "http://seerr.lan:5055"
SEERR_API_KEY: Final = "seerr-api-key-0123456789"
SEERR_VERSION: Final = "2.7.3"


def _json(payload: object, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=payload)


def seerr_user(
    user_id: int,
    display_name: str,
    *,
    jellyfin_user_id: str | None = None,
    plex_id: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A user as Seerr serialises one."""
    row: dict[str, Any] = {
        "id": user_id,
        "displayName": display_name,
        "email": f"{display_name.lower()}@example.test",
        "permissions": 2,
    }
    if jellyfin_user_id is not None:
        row["jellyfinUserId"] = jellyfin_user_id
        row["jellyfinUsername"] = display_name
    if plex_id is not None:
        row["plexId"] = plex_id
        row["plexUsername"] = display_name
    return row | fields


@dataclass
class FakeSeerr:
    """Seerr, answering from memory."""

    api_key: str = SEERR_API_KEY
    version: str = SEERR_VERSION
    users: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: ``(kind, tmdb id) -> the ``mediaInfo.status`` the backend reports``.
    availability: dict[tuple[str, int], int] = field(default_factory=dict[tuple[str, int], int])
    #: Titles the backend has no record of at all (no ``mediaInfo``).
    #: What ``POST /api/v1/request`` answers next: ``(status, payload)``.
    request_answer: tuple[int, dict[str, Any]] = (
        201,
        {"id": 7, "status": 2, "media": {"status": 2}},
    )
    #: ``path -> status`` forced on the next call to that path.
    fails: dict[str, int] = field(default_factory=dict[str, int])
    #: Paths that answer a body which is not JSON.
    garbage: set[str] = field(default_factory=set[str])
    offline: bool = False
    #: Set to answer ``/api/v1/user`` with ``401``, as a wrong key would.
    key_refused: bool = False
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the adapter talks through."""
        return httpx2.MockTransport(self.handle)

    def request_of(self, path: str) -> httpx2.Request:
        """The last request the fake received for ``path``."""
        return next(r for r in reversed(self.requests) if r.url.path == path)

    def handle(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("the request backend is unreachable")
        path = request.url.path
        forced = self.fails.pop(path, None)
        if forced is not None:
            return _json({"message": "forced"}, forced)
        if path in self.garbage:
            return httpx2.Response(200, content=b"<html>not json")
        if path == "/api/v1/status":
            return _json({"version": self.version, "commitTag": "local"})
        if request.headers.get("x-api-key") != self.api_key:
            return _json({"message": "Unauthorized"}, 401)
        if path == "/api/v1/user":
            return self._users(request)
        if path == "/api/v1/request" and request.method == "POST":
            status, payload = self.request_answer
            return _json(payload, status)
        return self._title(path)

    def _users(self, request: httpx2.Request) -> httpx2.Response:
        if self.key_refused:
            return _json({"message": "Unauthorized"}, 401)
        query = parse_qs(request.url.query.decode())
        take = int(query.get("take", ["100"])[0])
        skip = int(query.get("skip", ["0"])[0])
        page = self.users[skip : skip + take]
        return _json(
            {
                "pageInfo": {"pages": 1, "pageSize": take, "results": len(self.users)},
                "results": page,
            }
        )

    def _title(self, path: str) -> httpx2.Response:
        parts = path.strip("/").split("/")
        expected = 4
        if len(parts) != expected or parts[2] not in ("movie", "tv") or not parts[3].isdigit():
            return _json({"message": "Not found"}, 404)
        status = self.availability.get((parts[2], int(parts[3])))
        if status is None:
            return _json({"id": int(parts[3])})
        return _json({"id": int(parts[3]), "mediaInfo": {"id": 1, "status": status}})


def body_of(request: httpx2.Request) -> Mapping[str, Any]:
    """Return the JSON body a request carried."""
    try:
        payload: Any = json.loads(request.content or b"{}")
    except ValueError:
        return {}
    return cast("Mapping[str, Any]", payload) if isinstance(payload, dict) else {}
