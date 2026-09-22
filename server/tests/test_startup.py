import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tindarr.core.config import ServerConfig
from tindarr.core.keys import KEY_FILE_NAME, SecretKeyError
from tindarr.main.app import create_app, start
from tindarr.storage.migrate import SchemaTooNewError

pytestmark = pytest.mark.anyio


async def test_restart_reuses_the_key_and_decrypts_secrets(data_dir: Path) -> None:
    config = ServerConfig(data_dir=data_dir)
    first = await start(config)
    await first.services.settings.set("media_server_api_key", "api-key-0123456789")
    await first.engine.dispose()
    key = (data_dir / KEY_FILE_NAME).read_text()

    second = await start(config)
    value = await second.services.settings.get("media_server_api_key")
    await second.engine.dispose()
    assert value.value == "api-key-0123456789"
    assert (data_dir / KEY_FILE_NAME).read_text() == key


async def test_environment_key_is_not_written_to_disk(data_dir: Path) -> None:
    runtime = await start(ServerConfig(data_dir=data_dir, secret_key="e" * 40))  # pyright: ignore[reportArgumentType]
    await runtime.engine.dispose()
    assert not (data_dir / KEY_FILE_NAME).exists()


async def test_data_dir_is_created_private(tmp_path: Path) -> None:
    data_dir = tmp_path / "new" / "data"
    runtime = await start(ServerConfig(data_dir=data_dir))
    await runtime.engine.dispose()
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


async def test_changed_key_cannot_read_old_secrets(data_dir: Path) -> None:
    first = await start(ServerConfig(data_dir=data_dir, secret_key="a" * 40))  # pyright: ignore[reportArgumentType]
    await first.services.settings.set("media_server_api_key", "api-key-0123456789")
    await first.engine.dispose()
    second = await start(ServerConfig(data_dir=data_dir, secret_key="b" * 40))  # pyright: ignore[reportArgumentType]
    try:
        with pytest.raises(Exception, match="another key"):
            await second.services.settings.get("media_server_api_key")
    finally:
        await second.engine.dispose()


def test_unsafe_key_file_prevents_startup(data_dir: Path) -> None:
    key_file = data_dir / KEY_FILE_NAME
    key_file.write_text("k" * 40)
    key_file.chmod(0o644)
    with pytest.raises(SecretKeyError), TestClient(create_app(ServerConfig(data_dir=data_dir))):
        pass


def test_newer_schema_prevents_startup(data_dir: Path) -> None:
    import sqlite3  # noqa: PLC0415
    from contextlib import closing  # noqa: PLC0415

    config = ServerConfig(data_dir=data_dir)
    with TestClient(create_app(config)):
        pass
    with closing(sqlite3.connect(data_dir / "tindarr.db")) as connection, connection:
        connection.execute("UPDATE alembic_version SET version_num = 'ffff'")
    with pytest.raises(SchemaTooNewError), TestClient(create_app(config)):
        pass
