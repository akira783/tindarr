"""Shared fixtures."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock, FakeMediaServers
from tindeerr.auth.mediaserver import MediaServerConnector
from tindeerr.auth.ratelimit import RateLimits
from tindeerr.auth.sessions import SessionService
from tindeerr.auth.setup import SetupService
from tindeerr.auth.tokens import AccessTokens
from tindeerr.core.config import ServerConfig
from tindeerr.core.crypto import SecretCipher
from tindeerr.core.keys import KeyMaterial, KeyPurpose, load_key_material
from tindeerr.core.logs import clear_registered_secrets
from tindeerr.main.app import create_app
from tindeerr.storage.db import create_async_db_engine, database_path
from tindeerr.storage.migrate import upgrade_database
from tindeerr.storage.settings import SettingsStore


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hide the developer's TINDEERR_* variables and forget registered secrets."""
    for key in list(os.environ):
        if key.startswith("TINDEERR_"):
            monkeypatch.delenv(key)
    yield
    clear_registered_secrets()


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
    return ServerConfig(data_dir=data_dir)


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
