"""Shared fixtures."""

import io
import logging
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock, FakeMediaServers, server_config
from tindarr.auth.mediaserver import MediaServerConnector
from tindarr.auth.ratelimit import RateLimits
from tindarr.auth.sessions import SessionService
from tindarr.auth.setup import SetupService
from tindarr.auth.tokens import AccessTokens
from tindarr.core.config import ServerConfig
from tindarr.core.crypto import SecretCipher
from tindarr.core.keys import KeyMaterial, KeyPurpose, load_key_material
from tindarr.core.logs import build_handler, clear_registered_secrets, quiet_noisy_libraries
from tindarr.main.app import create_app
from tindarr.storage.db import create_async_db_engine, database_path
from tindarr.storage.migrate import upgrade_database
from tindarr.storage.settings import SettingsStore


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hide the developer's TINDARR_* variables and forget registered secrets."""
    for key in list(os.environ):
        if key.startswith("TINDARR_"):
            monkeypatch.delenv(key)
    yield
    clear_registered_secrets()


@pytest.fixture(autouse=True)
def pristine_logging() -> Iterator[None]:
    """Put the root logger back the way each test found it.

    ``configure_logging`` clears the root handlers and installs its own, bound to
    whatever ``sys.stdout`` was at that moment — which, inside a captured test, is a
    stream pytest closes when the test ends. Any command-line test that reaches it
    therefore leaves a handler behind that later tests still log through: it writes to a
    closed file, and, worse, its redaction filter *mutates the records on its way past*,
    clearing the ``exc_info`` a later test was about to assert on. That is exactly the
    failure that made ``test_jobs`` pass alone and fail in the suite.

    Restoring the handlers here fixes the whole class of it, rather than the one test
    that noticed.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    access = logging.getLogger("uvicorn.access")
    disabled = access.disabled
    try:
        yield
    finally:
        root.handlers, root.level = handlers, level
        access.disabled = disabled


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def config(data_dir: Path) -> ServerConfig:
    return server_config(data_dir)


@pytest.fixture
def client(config: ServerConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config)) as test_client:
        yield test_client


@pytest.fixture
async def engine(data_dir: Path) -> AsyncIterator[AsyncEngine]:
    """Async engine on a freshly migrated database."""
    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    async_engine = create_async_db_engine(db_path)
    yield async_engine
    await async_engine.dispose()


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Capture every log line through the real redacting handler, at DEBUG level."""
    stream = io.StringIO()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    root.handlers = [build_handler(stream)]
    root.setLevel(logging.DEBUG)
    quiet_noisy_libraries()
    try:
        yield stream
    finally:
        root.handlers, root.level = handlers, level


@pytest.fixture
def clock() -> FakeClock:
    """A clock the test moves itself."""
    return FakeClock()


@pytest.fixture
def keys(data_dir: Path) -> KeyMaterial:
    """Key material from a fixed master key, so key ids are stable inside a test."""
    return load_key_material("t" * 40, data_dir)


@pytest.fixture
def access_tokens(keys: KeyMaterial, clock: FakeClock) -> AccessTokens:
    """Access token codec of the install ``install-1``."""
    return AccessTokens(keys.derive(KeyPurpose.JWT_SIGNING), keys.key_id, "install-1", clock)


@pytest.fixture
def limits(clock: FakeClock) -> RateLimits:
    """Fresh rate limiters on the test's clock."""
    return RateLimits(clock)


@pytest.fixture
def sessions(engine: AsyncEngine, clock: FakeClock, access_tokens: AccessTokens) -> SessionService:
    """Session service on the test database."""
    return SessionService(engine, clock, access_tokens)


@pytest.fixture
def settings_store(engine: AsyncEngine, keys: KeyMaterial) -> SettingsStore:
    """Settings store with nothing forced by the environment."""
    return SettingsStore(engine, SecretCipher.for_settings(keys), {})


@pytest.fixture
def media_servers() -> FakeMediaServers:
    """Factory returning one fake media server."""
    return FakeMediaServers()


@pytest.fixture
def connector(
    engine: AsyncEngine, settings_store: SettingsStore, media_servers: FakeMediaServers
) -> MediaServerConnector:
    """Media server connector talking to the fake adapter."""
    return MediaServerConnector(engine, settings_store, media_servers)


@pytest.fixture
def setup_service(  # noqa: PLR0913, PLR0917 - the service's collaborators
    engine: AsyncEngine,
    data_dir: Path,
    clock: FakeClock,
    sessions: SessionService,
    connector: MediaServerConnector,
    settings_store: SettingsStore,
    limits: RateLimits,
) -> SetupService:
    """First-run setup service on the test database and data directory."""
    return SetupService(engine, data_dir, clock, sessions, connector, settings_store, limits)
