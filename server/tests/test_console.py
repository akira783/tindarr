"""Serving the web console: SPA fallback, hashed assets and headers per path.

Check (a) of the roadmap: "security headers per path, SPA fallback never under /api".
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.support import CONSOLE_ORIGIN, build_app, console_client
from tindarr.api.console import (
    ASSET_CACHE_CONTROL,
    CONSOLE_CSP,
    INDEX_CACHE_CONTROL,
    STATIC_CACHE_CONTROL,
    WebConsole,
    is_console_path,
    media_type_of,
    relative_path,
)
from tindarr.api.middleware import CONTENT_SECURITY_POLICY
from tindarr.core.config import DEFAULT_WEB_DIR, ServerConfig

INDEX_BODY = (
    '<!doctype html><html><head></head><body><div id="root"></div>'
    '<script type="module" src="/assets/index-abc123.js"></script></body></html>'
)


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    """A directory holding a built console, as the image's Node stage produces one."""
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_BODY, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("export const a = 1;\n", encoding="utf-8")
    (root / "assets" / "index-abc123.css").write_text(":root{color:red}\n", encoding="utf-8")
    (root / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    return root


@pytest.fixture
def console(data_dir: Path, web_dir: Path) -> Iterator[TestClient]:
    """A client on a server that serves the built console."""
    with console_client(build_app(data_dir, web_dir=web_dir)) as client:
        yield client


# --- the directory ------------------------------------------------------------------


def test_the_default_web_dir_is_the_one_the_image_uses() -> None:
    assert str(DEFAULT_WEB_DIR) == "/app/web"
    assert ServerConfig().web_dir == DEFAULT_WEB_DIR


def test_without_a_build_every_console_path_is_a_problem(data_dir: Path, tmp_path: Path) -> None:
    # Development: the Vite dev server serves the pages and proxies /api here, and the
    # server behaves as if the console module were not there.
    with console_client(build_app(data_dir, web_dir=tmp_path / "absent")) as client:
        for path in ("/", "/settings", "/assets/index-abc123.js"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.headers["content-type"] == "application/problem+json"
            assert response.json()["code"] == "not_found"


def test_a_console_built_after_startup_is_picked_up(data_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "late"
    with console_client(build_app(data_dir, web_dir=root)) as client:
        assert client.get("/").status_code == 404
        root.mkdir()
        (root / "index.html").write_text(INDEX_BODY, encoding="utf-8")
        assert client.get("/").status_code == 200


# --- what the console answers -------------------------------------------------------


def test_the_root_serves_index_html(console: TestClient) -> None:
    response = console.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert '<div id="root"></div>' in response.text


@pytest.mark.parametrize(
    "path", ["/settings", "/users", "/connect-phone", "/setup", "/sign-in/deep/link"]
)
def test_unknown_paths_fall_back_to_index_html(console: TestClient, path: str) -> None:
    response = console.get(path)
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text


def test_head_on_the_console_carries_the_same_headers(console: TestClient) -> None:
    response = console.head("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == INDEX_CACHE_CONTROL
    assert response.text == ""


def test_hashed_assets_are_served_and_cached_forever(console: TestClient) -> None:
    response = console.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL


def test_a_missing_asset_is_a_real_404_not_the_page(console: TestClient) -> None:
    # A stale client asking for a removed chunk must not be handed HTML to execute.
    response = console.get("/assets/index-gone.js")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert "root" not in response.text


def test_other_static_files_are_served_and_revalidated(console: TestClient) -> None:
    response = console.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == STATIC_CACHE_CONTROL
    assert console.get("/favicon.svg").headers["content-type"] == "image/svg+xml"


# --- what the console never answers -------------------------------------------------


def test_the_fallback_never_applies_under_api(console: TestClient) -> None:
    response = console.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("path", ["//api/v1/server/info", "//api/v1/nope", "//healthz"])
def test_a_doubled_slash_does_not_reach_the_console(console: TestClient, path: str) -> None:
    # Nothing collapses these before the console sees them, and answering one with the
    # page would put an API path behind the looser console policy. The URL is given in
    # full, because a client would otherwise read `//…` as a scheme-relative address.
    response = console.request("GET", CONSOLE_ORIGIN + path)
    assert response.request.url.path == path
    assert "text/html" not in response.headers["content-type"]
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "root" not in response.text


def test_the_api_keeps_its_own_answers(console: TestClient) -> None:
    assert console.get("/healthz").json() == {"status": "ok"}
    assert console.get("/api/v1/server/info").status_code == 200
    # A wrong method on a real route is still a 405, not the console's page.
    assert console.delete("/api/v1/server/info").status_code == 405
    assert console.post("/healthz").status_code == 405


def test_unsafe_methods_never_reach_the_console(console: TestClient) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = console.request(method, "/settings")
        assert response.status_code == 404, method
        assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    ("path", "served"),
    [
        ("/", True),
        ("/settings", True),
        ("/apixyz", True),
        ("/healthz", False),
        ("/api", False),
        ("/api/v1/server/info", False),
        ("/api/docs", False),
        # Nothing collapses these before the check, and answering them with the page
        # would put an API path behind the looser console policy.
        ("//api/v1/server/info", False),
        ("/healthz/", False),
        ("//healthz", False),
        ("/./api/v1/server/info", False),
    ],
)
def test_reserved_paths_are_not_the_consoles(path: str, served: bool) -> None:
    assert is_console_path(path) is served


# --- headers per path ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_index_html_carries_the_console_policy(console: TestClient, path: str) -> None:
    # `/index.html` is the same page as `/`, so it must not be served as a static file
    # — that branch carries no policy at all.
    response = console.get(path)
    assert response.headers["content-security-policy"] == CONSOLE_CSP
    assert response.headers["cache-control"] == INDEX_CACHE_CONTROL
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_the_console_policy_is_the_one_adr_0009_decided() -> None:
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' https://image.tmdb.org",
        "connect-src 'self'",
        "font-src 'self'",
        # Roadmap 4.6: the swipe page's trailer, and no other frame source.
        "frame-src https://www.youtube-nocookie.com",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "require-trusted-types-for 'script'",
    ):
        assert directive in CONSOLE_CSP
    assert "'unsafe-inline'" not in CONSOLE_CSP
    assert "'unsafe-eval'" not in CONSOLE_CSP
    # The player is framed, never scripted into this document.
    assert "script-src 'self';" in CONSOLE_CSP
    assert CONSOLE_CSP.count("youtube") == 1


def test_assets_and_the_api_keep_the_strict_policy(console: TestClient) -> None:
    for path in ("/assets/index-abc123.js", "/robots.txt", "/api/v1/server/info", "/healthz"):
        assert console.get(path).headers["content-security-policy"] == CONTENT_SECURITY_POLICY, path


def test_hsts_reaches_the_console_too(data_dir: Path, web_dir: Path) -> None:
    with console_client(build_app(data_dir, web_dir=web_dir, hsts=True)) as client:
        assert client.get("/").headers["strict-transport-security"] == "max-age=31536000"


def test_an_unknown_host_is_refused_for_the_console_as_well(data_dir: Path, web_dir: Path) -> None:
    with console_client(build_app(data_dir, web_dir=web_dir), host="evil.example") as client:
        response = client.get("/")
        assert response.status_code == 400
        assert response.json()["code"] == "host_not_allowed"


# --- path safety --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", ""),
        ("/index.html", "index.html"),
        ("/assets//a.js", "assets/a.js"),
        ("/./assets/a.js", "assets/a.js"),
        ("/../etc/passwd", None),
        ("/assets/../../etc/passwd", None),
        ("/a\\b", None),
        ("/a\x00b", None),
    ],
)
def test_path_segments_are_checked_before_touching_the_file_system(
    path: str, expected: str | None
) -> None:
    assert relative_path(path) == expected


def test_a_symlink_out_of_the_web_directory_is_not_served(web_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.key"
    outside.write_text("super secret", encoding="utf-8")
    (web_dir / "assets" / "leak.js").symlink_to(outside)
    response = WebConsole(web_dir).response("/assets/leak.js")
    assert response.status_code == 404


def test_unknown_extensions_are_served_as_bytes(tmp_path: Path) -> None:
    assert media_type_of(tmp_path / "x.bin") == "application/octet-stream"
    assert media_type_of(tmp_path / "X.CSS") == "text/css; charset=utf-8"
