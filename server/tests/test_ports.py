"""The media server port: value types and the user id normalisation of docs/auth.md §4."""

import pytest

from tests.support import FakeMediaServer
from tindarr.ports.media_server import (
    ConnectionCheck,
    MediaServer,
    ServerIdentity,
    as_media_server_kind,
    normalize_user_id,
)


def test_the_fake_media_server_implements_the_port() -> None:
    # Checked by the type checker; the assertion keeps it honest at runtime too.
    server: MediaServer = FakeMediaServer()
    assert server.kind == "jellyfin"


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("jellyfin", "8A1B2C3D4E5F60718293A4B5C6D7E8F9", "8a1b2c3d4e5f60718293a4b5c6d7e8f9"),
        ("jellyfin", "8a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9", "8a1b2c3d4e5f60718293a4b5c6d7e8f9"),
        ("emby", " 8a1b2c3d4e5f60718293a4b5c6d7e8f9 ", "8a1b2c3d4e5f60718293a4b5c6d7e8f9"),
        ("plex", "123456", "123456"),
        ("plex", "0123456", "123456"),
    ],
)
def test_user_ids_are_normalised(kind: str, raw: str, expected: str) -> None:
    assert normalize_user_id(kind, raw) == expected  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("jellyfin", "not-hex"),
        ("jellyfin", "8a1b2c3d"),
        ("emby", ""),
        ("plex", "alex@example.com"),
        ("plex", "12a"),
    ],
)
def test_an_unexpected_user_id_is_refused(kind: str, raw: str) -> None:
    with pytest.raises(ValueError, match="id must be"):
        normalize_user_id(kind, raw)  # pyright: ignore[reportArgumentType]


def test_the_identity_key_is_what_is_stored() -> None:
    identity = ServerIdentity(kind="plex", server_id="abc123", name="Home", version="1.41")
    assert identity.key == "plex:abc123"


def test_connection_health() -> None:
    assert ConnectionCheck("ok").ok
    assert not ConnectionCheck("unauthorized").ok


def test_known_media_server_kinds() -> None:
    assert as_media_server_kind("jellyfin") == "jellyfin"
    assert as_media_server_kind("kodi") is None
    assert as_media_server_kind(None) is None
