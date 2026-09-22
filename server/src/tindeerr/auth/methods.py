"""Which sign-in methods the configured media server offers."""

from collections.abc import Mapping
from typing import Final, Literal

from tindeerr.ports.media_server import MediaServerKind

type AuthMethod = Literal["password", "plex_pin"]

_METHODS: Final[Mapping[MediaServerKind, tuple[AuthMethod, ...]]] = {
    "jellyfin": ("password",),
    "emby": ("password",),
    "plex": ("plex_pin",),
}


def sign_in_methods(kind: MediaServerKind | None) -> list[AuthMethod]:
    """Return the sign-in methods for a media server kind; none until one is configured."""
    if kind is None:
        return []
    return list(_METHODS[kind])
