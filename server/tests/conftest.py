"""Shared fixtures."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.core.config import ServerConfig
from tindeerr.core.logs import clear_registered_secrets
from tindeerr.main.app import create_app
from tindeerr.storage.db import create_async_db_engine, database_path
from tindeerr.storage.migrate import upgrade_database


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
