"""General server settings (docs/auth.md, sections 7 and 10).

Every field of ``ServerSettings`` is stored from step 2; ``name``, ``public_url`` and
``password_sign_in`` act now, the rest wait for the swipe engine. What makes this more
than a form is the two fields an ordinary administrator may not touch:

- ``password_sign_in`` decides whether passwords are forwarded to the media server at
  all, so only an administrator **of the media server** may change it;
- ``public_url`` is the address every paired phone will come back to, written into
  every QR code. It needs the same administrator **and** a re-authentication less than
  five minutes old, and the address has to prove it reaches this very server before it
  is stored (``tindarr.auth.publicurl``). Its host must already be one this server
  answers to — the console is normally open at it — because the proof is bound to the
  host the check's request arrives at; an unknown host is refused before any call is
  made, with the reason that says what to do about it.

A patch is applied as a whole: a locked field, a refused role or an unverified address
leaves everything unchanged, so the console never has to reason about half a save.
"""

from http import HTTPStatus

from fastapi import APIRouter

from tindarr.api.context import host_of
from tindarr.api.deps import Services
from tindarr.api.security import AdminSession
from tindarr.api.v1.models import (
    MEDIA_SERVER_ADMIN_FIELDS,
    REAUTH_FIELDS,
    SETTING_FOR_SETTINGS_FIELD,
    ServerSettingsPatchInput,
    ServerSettingsResponse,
    read_server_settings,
)
from tindarr.auth import access
from tindarr.auth.events import security_event
from tindarr.core.errors import ProblemError
from tindarr.storage.settings import SettingLockedError

router = APIRouter(tags=["admin", "console"])


@router.get(
    "/settings",
    operation_id="getSettings",
    summary="General server settings",
)
async def get_settings(services: Services, session: AdminSession) -> ServerSettingsResponse:
    """Return every general setting, and which of them the environment locks."""
    return await read_server_settings(services.settings)


@router.patch(
    "/settings",
    operation_id="updateSettings",
    summary="Change general server settings",
)
async def update_settings(
    payload: ServerSettingsPatchInput, services: Services, session: AdminSession
) -> ServerSettingsResponse:
    """Apply the fields the caller sent, all or nothing."""
    given = payload.model_fields_set
    if given & MEDIA_SERVER_ADMIN_FIELDS:
        access.require_media_server_admin(session.signed_in_user)
    if given & REAUTH_FIELDS:
        access.require_recent_reauth(session.session, services.clock.now())
    _refuse_locked(services, given)
    changes = payload.changes
    if payload.public_url is not None:
        # Checked before it is stored, and before the host policy accepts it: an
        # address that does not answer as this server is never written down.
        services.limits.connection_tests.hit(session.session.id)
        await _verify_public_url(services, payload.public_url)
    await services.settings.set_many(changes)
    if "public_url" in given:
        services.hosts.set_public_url(payload.public_url)
        security_event(
            "public_url_changed",
            user_id=session.signed_in_user.id,
            cleared=payload.public_url is None,
        )
    security_event(
        "settings_changed", user_id=session.signed_in_user.id, fields=",".join(sorted(given))
    )
    return await read_server_settings(services.settings)


async def _verify_public_url(services: Services, public_url: str) -> None:
    """Make the address prove it reaches this server, refusing an unknown host first.

    The proof is bound to the host the check's request arrives at, so a host this
    server does not answer to cannot produce one. Saying that up front — rather than
    calling the address and reporting "unexpected answer" — is the difference between
    an administrator adding it to ``TINDARR_ALLOWED_HOSTS`` and giving up.
    """
    host = host_of(public_url)
    if host is None or not services.hosts.allows_name(host):
        security_event("public_url_unverified", reason="host_not_allowed")
        raise ProblemError(
            HTTPStatus.CONFLICT,
            "public_url_unverified",
            "This server does not answer to that host name; add it to "
            "TINDARR_ALLOWED_HOSTS (or set TINDARR_PUBLIC_URL) and restart.",
            extensions={"reason": "host_not_allowed"},
        )
    await services.public_url.verify(public_url, host)


def _refuse_locked(services: Services, given: frozenset[str] | set[str]) -> None:
    """Refuse the whole patch when one of its fields is set by an environment variable.

    Done before the address is called, so a locked ``public_url`` never makes the
    server contact an address it is not going to store either way.
    """
    for field in sorted(given):
        setting = SETTING_FOR_SETTINGS_FIELD[field]
        if services.settings.is_locked(setting):
            raise SettingLockedError(setting)
