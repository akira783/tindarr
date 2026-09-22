import os
import stat
import threading
from pathlib import Path

import pytest

from tindeerr.core import keys
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


def test_surrounding_whitespace_is_ignored_whatever_the_source(tmp_path: Path) -> None:
    key = "w" * 40
    from_env = load_key_material(f"  {key}\r\n", tmp_path)
    path = tmp_path / KEY_FILE_NAME
    path.write_text(f"\n{key}  \n")
    path.chmod(0o600)
    from_file = load_key_material(None, tmp_path)
    assert from_env.secret == from_file.secret == key.encode()
    assert from_env.key_id == from_file.key_id


def test_whitespace_does_not_count_towards_the_length(tmp_path: Path) -> None:
    with pytest.raises(SecretKeyError, match="at least 32 characters"):
        load_key_material("k" * 31 + "   ", tmp_path)


def test_generated_key_file_is_never_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(keys.os, "link", crash)
    with pytest.raises(OSError, match="No space"):
        load_key_material(None, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_losing_the_creation_race_uses_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner = "v" * 43
    real_link = os.link

    def link_after_the_other_process(src: str, dst: str) -> None:
        Path(dst).write_text(winner + "\n")
        Path(dst).chmod(0o600)
        real_link(src, dst)

    monkeypatch.setattr(keys.os, "link", link_after_the_other_process)
    material = load_key_material(None, tmp_path)
    assert material.secret == winner.encode()
    assert material.source == "file"
    assert [path.name for path in tmp_path.iterdir()] == [KEY_FILE_NAME]


def test_concurrent_first_starts_agree_on_one_key(tmp_path: Path) -> None:
    barrier = threading.Barrier(8)
    results: list[bytes] = []

    def start() -> None:
        barrier.wait()
        results.append(load_key_material(None, tmp_path).secret)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(results) == 8
    assert len(set(results)) == 1
    assert (tmp_path / KEY_FILE_NAME).read_text().strip().encode() == results[0]
    assert [path.name for path in tmp_path.iterdir()] == [KEY_FILE_NAME]


@pytest.mark.parametrize("content", ["", "\n", "   \n"])
def test_leftover_empty_key_file_is_reported(tmp_path: Path, content: str) -> None:
    path = tmp_path / KEY_FILE_NAME
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(SecretKeyError, match="is empty; delete it"):
        load_key_material(None, tmp_path)
    assert path.read_text() == content


def test_partial_key_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / KEY_FILE_NAME
    path.write_text("abc123")
    path.chmod(0o600)
    with pytest.raises(SecretKeyError, match="at least 32 characters"):
        load_key_material(None, tmp_path)


def test_key_file_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_key_material(None, tmp_path)
    monkeypatch.setattr(keys.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(SecretKeyError, match="owned by uid"):
        load_key_material(None, tmp_path)


def test_fifo_in_place_of_the_key_file_is_refused_without_hanging(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / KEY_FILE_NAME, 0o600)
    with pytest.raises(SecretKeyError, match="not a regular file"):
        load_key_material(None, tmp_path)


def test_directory_in_place_of_the_key_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / KEY_FILE_NAME).mkdir(mode=0o700)
    with pytest.raises(SecretKeyError, match="not a regular file"):
        load_key_material(None, tmp_path)


def test_symlinked_key_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("s" * 40)
    target.chmod(0o600)
    (tmp_path / KEY_FILE_NAME).symlink_to(target)
    with pytest.raises(SecretKeyError, match="symbolic link"):
        load_key_material(None, tmp_path)
