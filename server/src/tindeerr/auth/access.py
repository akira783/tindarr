"""Who may do what: roles, remote access and step-up re-authentication.

Pure checks on rows the caller already loaded (docs/auth.md, sections 2, 4 and 7). They
raise the contract's problems, so every endpoint answers the same way.
"""

from datetime import datetime, timedelta
from typing import Final

from tindeerr.auth import errors
from tindeerr.storage.sessions import Session
from tindeerr.storage.users import User

#: How long a sign-in or a ``POST /auth/web/reauth`` counts as fresh.
REAUTH_WINDOW: Final = timedelta(minutes=5)


def require_usable_account(user: User) -> None:
    """401 unless the user is enabled and still linked to a media server account."""
    if not user.can_sign_in:
        raise errors.unauthorized("This account can no longer be used.")


def require_remote_access(user: User, *, client_is_private: bool) -> None:
    """403 ``remote_access_denied`` for a restricted user from outside the local network.

    Jellyfin and Emby only ever see Tindeerr's own address, so Tindeerr enforces their
    ``EnableRemoteAccess`` policy itself, at sign-in and on every later request.
    """
    if not user.remote_access and not client_is_private:
        raise errors.remote_access_denied()


def require_admin(user: User) -> None:
    """403 ``admin_required`` unless the effective role is ``admin``."""
    if not user.is_admin:
        raise errors.admin_required()


def require_media_server_admin(user: User) -> None:
    """403 unless the user administers the media server itself (a promotion is not enough)."""
    require_admin(user)
    if not user.media_server_admin:
        raise errors.media_server_admin_required()


def reauth_expires_at(session: Session, now: datetime) -> datetime | None:
    """Until when this session counts as freshly re-authenticated, or ``None``."""
    if session.reauth_at is None:
        return None
    expires_at = session.reauth_at + REAUTH_WINDOW
    return expires_at if expires_at > now else None


def require_recent_reauth(session: Session, now: datetime) -> None:
    """403 ``reauth_required`` unless the session re-authenticated in the last 5 minutes."""
    if reauth_expires_at(session, now) is None:
        raise errors.reauth_required()
