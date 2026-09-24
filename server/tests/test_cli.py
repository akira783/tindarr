import asyncio
import logging
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from tindarr import __version__
from tindarr.main import cli


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def health_server() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_healthcheck_succeeds_when_healthz_answers(
    monkeypatch: pytest.MonkeyPatch, health_server: int
) -> None:
    monkeypatch.setenv("TINDARR_HOST", "0.0.0.0")  # noqa: S104 - probed on loopback
    monkeypatch.setenv("TINDARR_PORT", str(health_server))
    assert cli.main(["healthcheck"]) == 0


def test_healthcheck_fails_when_nothing_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINDARR_PORT", str(free_port()))
    assert cli.main(["healthcheck"]) == 1


def test_healthcheck_ignores_http_proxies(
    monkeypatch: pytest.MonkeyPatch, health_server: int
) -> None:
    dead_proxy = f"http://127.0.0.1:{free_port()}"
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, dead_proxy)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TINDARR_PORT", str(health_server))
    assert cli.main(["healthcheck"]) == 0


def test_healthcheck_handles_ipv6_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    class FakeOpener:
        def open(self, url: str, timeout: float) -> Any:
            urls.append(url)
            raise OSError

    def fake_build_opener(*handlers: object) -> FakeOpener:
        return FakeOpener()

    monkeypatch.setattr(cli.urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setenv("TINDARR_HOST", "::1")
    assert cli.main(["healthcheck"]) == 1
    assert urls == ["http://[::1]:8787/healthz"]


def test_invalid_configuration_exits_with_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging  # noqa: PLC0415

    root = logging.getLogger()
    saved = list(root.handlers)
    monkeypatch.setenv("TINDARR_PORT", "not-a-port")
    try:
        assert cli.main([]) == 2
    finally:
        root.handlers = saved
        logging.getLogger("uvicorn.access").disabled = False
        logging.captureWarnings(capture=False)
    output = capsys.readouterr().out
    assert "TINDARR_PORT" in output
    assert "not-a-port" not in output


def test_invalid_setting_override_exits_with_2_before_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging  # noqa: PLC0415

    def fail_run(app: object, **kwargs: Any) -> None:
        pytest.fail("the server must not start")

    root = logging.getLogger()
    saved = list(root.handlers)
    monkeypatch.setattr(cli.uvicorn, "run", fail_run)
    monkeypatch.setenv("TINDARR_MEDIA_SERVER_KIND", "kodi-hunter2")
    try:
        assert cli.main(["serve"]) == 2
    finally:
        root.handlers = saved
        logging.getLogger("uvicorn.access").disabled = False
        logging.captureWarnings(capture=False)
    output = capsys.readouterr().out
    assert "TINDARR_MEDIA_SERVER_KIND" in output
    assert "hunter2" not in output


def test_serve_runs_uvicorn_without_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(app: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    import logging  # noqa: PLC0415

    root = logging.getLogger()
    saved = list(root.handlers)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setenv("TINDARR_PORT", "9999")
    try:
        assert cli.main(["serve"]) == 0
    finally:
        root.handlers = saved
        logging.getLogger("uvicorn.access").disabled = False
        logging.captureWarnings(capture=False)
    (kwargs,) = calls
    assert kwargs["port"] == 9999
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["proxy_headers"] is False
    assert kwargs["server_header"] is False
    assert kwargs["log_config"] is None
    # Requests the application holds open must not be able to pile up (M3).
    assert kwargs["limit_concurrency"] == cli.MAX_CONCURRENCY


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert __version__ in capsys.readouterr().out


def _not_a_terminal() -> bool:
    return False


def _ignore_level(level: str) -> None:
    """Keep the CLI from reconfiguring the logging the other tests rely on."""
    del level


def test_resetting_the_media_server_needs_a_confirmation(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TINDARR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "configure_logging", _ignore_level)
    monkeypatch.setattr(cli.sys.stdin, "isatty", _not_a_terminal)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["media-server", "reset"]) == 2
    assert "--yes" in caplog.text
    assert not (data_dir / "tindarr.db").exists()


def test_resetting_the_media_server_starts_setup_again(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tindarr.auth.setupcode import read_setup_code  # noqa: PLC0415
    from tindarr.core.config import ServerConfig  # noqa: PLC0415
    from tindarr.main.app import start  # noqa: PLC0415

    monkeypatch.setenv("TINDARR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "configure_logging", _ignore_level)

    async def prepare() -> str:
        runtime = await start(ServerConfig(data_dir=data_dir))
        try:
            await runtime.services.settings.set("media_server_url", "http://jellyfin.lan:8096")
            code = read_setup_code(runtime.services.setup.code_path)
            assert code is not None
            return code
        finally:
            await runtime.engine.dispose()

    first_code = asyncio.run(prepare())

    assert cli.main(["media-server", "reset", "--yes"]) == 0

    async def check() -> None:
        runtime = await start(ServerConfig(data_dir=data_dir))
        try:
            assert (await runtime.services.settings.get("media_server_url")).value is None
            state = await runtime.services.server_state.read()
            assert state.setup_completed_at is None
            assert state.media_server_identity is None
            assert read_setup_code(runtime.services.setup.code_path) != first_code
        finally:
            await runtime.engine.dispose()

    asyncio.run(check())


# --- tindarr import suggestarr ----------------------------------------------------------


def _fork_db(path: Path) -> Path:
    from tests.test_swipe_imports_suggestarr import _fork_database  # noqa: PLC0415

    return _fork_database(path)


def _seed_user(data_dir: Path, media_id: str = "media-1") -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    from tindarr.core.config import ServerConfig  # noqa: PLC0415
    from tindarr.main.app import start  # noqa: PLC0415
    from tindarr.storage.db import write_transaction  # noqa: PLC0415
    from tindarr.storage.users import insert as insert_user  # noqa: PLC0415
    from tindarr.storage.users import new_user  # noqa: PLC0415

    async def prepare() -> str:
        runtime = await start(ServerConfig(data_dir=data_dir))
        try:
            async with write_transaction(runtime.engine) as connection:
                user = await insert_user(
                    connection,
                    new_user(
                        media_id,
                        "alex",
                        datetime(2026, 9, 24, tzinfo=UTC),
                        admin=True,
                        remote=True,
                    ),
                )
            return user.id
        finally:
            await runtime.engine.dispose()

    return asyncio.run(prepare())


def _vote_count(data_dir: Path, user_id: str) -> int:
    from tindarr.core.config import ServerConfig  # noqa: PLC0415
    from tindarr.main.app import start  # noqa: PLC0415
    from tindarr.storage import votes as vote_repository  # noqa: PLC0415
    from tindarr.storage.db import write_transaction  # noqa: PLC0415

    async def count() -> int:
        runtime = await start(ServerConfig(data_dir=data_dir))
        try:
            async with write_transaction(runtime.engine) as connection:
                return await vote_repository.count(connection, user_id)
        finally:
            await runtime.engine.dispose()

    return asyncio.run(count())


def test_importing_the_fork_writes_the_votes_and_is_idempotent(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "configure_logging", _ignore_level)
    database = _fork_db(tmp_path / "requests.db")
    user_id = _seed_user(data_dir)

    assert cli.main(["import", "suggestarr", "--from", str(database), "--user", user_id]) == 0
    assert _vote_count(data_dir, user_id) == 4

    assert cli.main(["import", "suggestarr", "--from", str(database), "--user", user_id]) == 0
    assert _vote_count(data_dir, user_id) == 4


def test_a_dry_run_writes_nothing(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "configure_logging", _ignore_level)
    database = _fork_db(tmp_path / "requests.db")
    user_id = _seed_user(data_dir)

    assert (
        cli.main(["import", "suggestarr", "--from", str(database), "--user", user_id, "--dry-run"])
        == 0
    )

    assert _vote_count(data_dir, user_id) == 0


def test_importing_refuses_a_database_that_is_not_the_fork(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import sqlite3  # noqa: PLC0415

    monkeypatch.setenv("TINDARR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "configure_logging", _ignore_level)
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()

    with caplog.at_level(logging.ERROR):
        assert cli.main(["import", "suggestarr", "--from", str(other)]) == 2

    assert "swipe_votes" in caplog.text
    # It failed before touching the instance, so no database was created for it.
    assert not (data_dir / "tindarr.db").exists()


def test_importing_refuses_when_no_account_can_be_matched(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TINDARR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "configure_logging", _ignore_level)
    database = _fork_db(tmp_path / "requests.db")
    user_id = _seed_user(data_dir)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["import", "suggestarr", "--from", str(database)]) == 2

    assert "--user" in caplog.text
    assert _vote_count(data_dir, user_id) == 0
