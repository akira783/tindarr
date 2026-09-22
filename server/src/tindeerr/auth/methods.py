"""Which sign-in methods this caller may use (docs/auth.md, section 11).

A pure function of the configured media server, the ``password_sign_in`` setting, the
caller's network and two facts the media server adapters provide later: whether Quick
Connect is enabled (step 2b caches ``GET /QuickConnect/Enabled``) and whether pairing is
possible (step 2c, once ``public_url`` is set).
"""

from typing import Literal

from tindeerr.ports.media_server import MediaServerKind
from tindeerr.storage.settings import PasswordSignIn

type AuthMethod = Literal["password", "plex_pin", "quick_connect", "pairing"]


def password_allowed(setting: PasswordSignIn, *, client_is_private: bool) -> bool:
    """Whether this caller may sign in with a password."""
    return setting == "enabled" or (setting == "lan_only" and client_is_private)


def sign_in_methods(
    kind: MediaServerKind | None,
    *,
    password_sign_in: PasswordSignIn = "enabled",  # noqa: S107 - a setting value
    client_is_private: bool = False,
    quick_connect_enabled: bool | None = None,
    pairing_available: bool = False,
) -> list[AuthMethod]:
    """Return the methods offered to this caller; none until a media server is configured.

    ``quick_connect_enabled`` is ``None`` while the server has not been asked yet, and
    the method is then left out.
    """
    if kind is None:
        return []
    methods: list[AuthMethod] = []
    if kind == "plex":
        methods.append("plex_pin")
    else:
        if password_allowed(password_sign_in, client_is_private=client_is_private):
            methods.append("password")
        if kind == "jellyfin" and quick_connect_enabled:
            methods.append("quick_connect")
    if pairing_available:
        methods.append("pairing")
    return methods
