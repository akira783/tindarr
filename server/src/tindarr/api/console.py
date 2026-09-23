"""Serving the built web console (docs/architecture.md, "Web console"; ADR 0009).

The server answers three kinds of path:

- ``/api/…`` and ``/healthz`` are the API and the container's probe. The console never
  sees them: an unknown API path stays a ``404`` problem, and a wrong method on a real
  route stays a ``405``;
- ``/assets/…`` are the hashed files Vite emits. They are immutable for a year, and a
  missing one is a real ``404`` — never ``index.html``, which would make a stale client
  execute HTML as JavaScript;
- anything else that is a ``GET`` or a ``HEAD`` gets the file if there is one
  (``favicon.svg``, ``robots.txt``), and ``index.html`` otherwise, so client-side
  routing works on a reload or a shared link.

``index.html`` carries the console's own Content-Security-Policy (ADR 0009) and is never
cached; every other response keeps the strict API policy from
``tindarr.api.middleware``, which only fills a policy in when the response has none.

**Path handling.** The ASGI path is already percent-decoded, so it is split into
segments here and refused if any of them is ``.`` or ``..``; the resolved file must also
stay inside the console directory, which stops a symbolic link planted in it from
serving ``/etc/passwd``.

**Development.** With no built console in ``TINDARR_WEB_DIR`` (the default is
``/app/web``, where the image puts it), nothing is intercepted at all and every path
outside the API answers the router's own ``404`` problem: the Vite dev server serves the
pages and proxies ``/api`` here.
"""

import logging
from http import HTTPStatus
from pathlib import Path
from typing import Final

from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from tindarr.api.errors import problem_response

INDEX_FILE: Final = "index.html"
#: Hashed build output: cached forever, and never replaced by the SPA fallback.
ASSETS_SEGMENT: Final = "assets"
#: Paths the console never answers to, whatever the method.
RESERVED_PREFIXES: Final = ("/api",)
RESERVED_PATHS: Final = frozenset({"/healthz"})
#: Methods the console serves; everything else falls through to the API's own answer.
SERVED_METHODS: Final = frozenset({"GET", "HEAD"})

#: The console's policy (ADR 0009). No inline script or style, no third-party origin;
#: ``img-src`` allows TMDb because later steps show poster images in the console.
CONSOLE_CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' https://image.tmdb.org; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "require-trusted-types-for 'script'"
)
INDEX_CACHE_CONTROL: Final = "no-store"
ASSET_CACHE_CONTROL: Final = "public, max-age=31536000, immutable"
#: Files served from the root (``favicon.svg``, ``robots.txt``): revalidated every time.
STATIC_CACHE_CONTROL: Final = "no-cache"

#: Media types are decided here rather than from the system's ``mime.types``, so the
#: same build is served identically on every host, with an explicit charset.
MEDIA_TYPES: Final[dict[str, str]] = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/vnd.microsoft.icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
}
FALLBACK_MEDIA_TYPE: Final = "application/octet-stream"

logger = logging.getLogger(__name__)


def is_console_path(path: str) -> bool:
    """Whether the console may answer this path (the API and the probe keep theirs).

    Decided on the path's segments, not on its text: nothing collapses ``//api/v1/…``
    or strips the slash from ``/healthz/`` before this, and answering either with the
    console page would put an API path behind the looser console policy.
    """
    segments = relative_path(path)
    if segments is None:
        return True
    reserved = {prefix.lstrip("/") for prefix in RESERVED_PREFIXES}
    reserved |= {reserved_path.lstrip("/") for reserved_path in RESERVED_PATHS}
    first = segments.split("/", 1)[0]
    return first not in reserved


def relative_path(path: str) -> str | None:
    """Return the path as a safe relative file path, or ``None`` when it is not one.

    The ASGI path is already decoded, so ``..`` and ``.`` segments, backslashes and NUL
    are refused here rather than trusted to the file system.
    """
    if "\\" in path or "\x00" in path:
        return None
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            return None
        segments.append(segment)
    return "/".join(segments)


def media_type_of(path: Path) -> str:
    """Return the media type served for this file."""
    return MEDIA_TYPES.get(path.suffix.lower(), FALLBACK_MEDIA_TYPE)


class WebConsole:
    """The built single-page console, served from one directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """The directory the console is served from."""
        return self._root

    @property
    def available(self) -> bool:
        """Whether a built console is there to serve (checked on every request).

        A server started before its console was built therefore starts serving it
        without a restart, and a server with no console at all behaves as if this
        module did not exist.
        """
        return self._file(INDEX_FILE) is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer one console request."""
        await self.response(scope["path"])(scope, receive, send)

    def response(self, path: str) -> Response:
        """Build the response for a console path (the SPA rules of the architecture)."""
        relative = relative_path(path)
        if relative is None:
            # A path the console cannot map to a file (``..``, a backslash, a NUL).
            return _not_found()
        if relative.split("/", 1)[0] == ASSETS_SEGMENT:
            asset = self._file(relative)
            # A hashed asset that is gone is gone: never answer it with the page.
            return _not_found() if asset is None else _file_response(asset, ASSET_CACHE_CONTROL)
        # `/index.html` is the page, not a static file: it goes through the branch
        # below, which is the one that carries the console's CSP.
        if relative and relative != INDEX_FILE:
            static = self._file(relative)
            if static is not None:
                return _file_response(static, STATIC_CACHE_CONTROL)
        index = self._file(INDEX_FILE)
        if index is None:
            return _not_found()
        return _file_response(index, INDEX_CACHE_CONTROL, csp=CONSOLE_CSP)

    def _file(self, relative: str) -> Path | None:
        """Return the file for a checked relative path, or ``None``.

        The resolved path must stay inside the console directory: a symbolic link
        pointing out of it is not served.
        """
        candidate = self._root / relative
        try:
            if not candidate.is_file():
                return None
            resolved = candidate.resolve(strict=True)
        except OSError:  # pragma: no cover - a race with a file being removed
            return None
        if not resolved.is_relative_to(self._root.resolve()):
            logger.warning("refused a console file outside the web directory")
            return None
        return resolved


def _file_response(path: Path, cache_control: str, csp: str | None = None) -> FileResponse:
    headers = {"Cache-Control": cache_control}
    if csp is not None:
        headers["Content-Security-Policy"] = csp
    return FileResponse(path, media_type=media_type_of(path), headers=headers)


def _not_found() -> Response:
    return problem_response(HTTPStatus.NOT_FOUND, "not_found")


class ConsoleMiddleware:
    """Answers the console's paths before the API router ever sees them.

    A middleware rather than a catch-all route: the API's own ``404`` and ``405``
    answers for unknown paths and wrong methods stay exactly as they were, and nothing
    the console does can shadow a route. It sits inside the host check and the security
    headers, so a console response carries them too.
    """

    def __init__(self, app: ASGIApp, console: WebConsole | None = None) -> None:
        self.app = app
        self.console = console

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the console, or hand the request to the API."""
        if (
            self.console is not None
            and scope["type"] == "http"
            and scope["method"] in SERVED_METHODS
            and is_console_path(scope["path"])
            and self.console.available
        ):
            await self.console(scope, receive, send)
            return
        await self.app(scope, receive, send)
