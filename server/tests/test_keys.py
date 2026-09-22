import stat
from pathlib import Path

import pytest

from tindeerr.core.keys import (
    KEY_FILE_NAME,
    KeyPurpose,
    SecretKeyError,
    load_key_material,
)

ENV_KEY = "k" * 32


def test_environment_key_is_used_and_nothing_is_written(tmp_path: Path) -> None:
    keys = load_key_material(ENV_KEY, tmp_path)
    assert keys.source == "environment"
    assert keys.secret == ENV_KEY.encode()
    assert not (tmp_path / KEY_FILE_NAME).exists()


def test_short_environment_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SecretKeyError, match="at least 32 characters"):
        load_key_material("k" * 31, tmp_path)


def test_key_is_generated_once_with_mode_0600(tmp_path: Path) -> None:
    first = load_key_material(None, tmp_path)
    path = tmp_path / KEY_FILE_NAME
    assert first.source == "generated"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(path.read_text().strip()) >= 32

    second = load_key_material(None, tmp_path)
    assert second.source == "file"
    assert second.secret == first.secret


def test_group_or_world_accessible_key_file_is_refused(tmp_path: Path) -> None:
    load_key_material(None, tmp_path)
    path = tmp_path / KEY_FILE_NAME
    path.chmod(0o640)
    with pytest.raises(SecretKeyError, match="chmod 600"):
        load_key_material(None, tmp_path)


def test_short_key_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / KEY_FILE_NAME
    path.write_text("short\n")
    path.chmod(0o600)
    with pytest.raises(SecretKeyError, match="at least 32 characters"):
        load_key_material(None, tmp_path)


def test_sub_keys_are_distinct_and_deterministic(tmp_path: Path) -> None:
    keys = load_key_material(ENV_KEY, tmp_path)
    settings_key = keys.derive(KeyPurpose.SETTINGS_ENCRYPTION)
    jwt_key = keys.derive(KeyPurpose.JWT_SIGNING)
    assert len(settings_key) == 32
    assert settings_key != jwt_key
    assert settings_key != keys.secret
    assert load_key_material(ENV_KEY, tmp_path).derive(KeyPurpose.SETTINGS_ENCRYPTION) == (
        settings_key
    )


def test_key_id_is_a_short_fingerprint(tmp_path: Path) -> None:
    keys = load_key_material(ENV_KEY, tmp_path)
    other = load_key_material("o" * 32, tmp_path)
    assert len(keys.key_id) == 8
    int(keys.key_id, 16)
    assert keys.key_id != other.key_id


def test_repr_never_shows_the_key(tmp_path: Path) -> None:
    keys = load_key_material(ENV_KEY, tmp_path)
    assert ENV_KEY not in repr(keys)
    assert ENV_KEY not in str(keys)
