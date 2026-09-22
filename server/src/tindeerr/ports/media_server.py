"""Media server port.

Only the value types needed so far live here; the ``MediaServer`` protocol arrives
with the first adapter (see docs/architecture.md).
"""

from typing import Final, Literal, get_args

type MediaServerKind = Literal["jellyfin", "emby", "plex"]

MEDIA_SERVER_KINDS: Final[tuple[MediaServerKind, ...]] = get_args(MediaServerKind.__value__)


def as_media_server_kind(value: object) -> MediaServerKind | None:
    """Return ``value`` as a media server kind, or ``None`` if it is not one."""
    return next((kind for kind in MEDIA_SERVER_KINDS if kind == value), None)
