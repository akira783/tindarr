"""Administration endpoints. Step 2b implements the media server connector.

docs/auth.md, section 6. Repointing the media server is the most dangerous thing an
administrator can do — a connector aimed at a server the attacker controls collects
everyone's password at their next sign-in and makes its owner an administrator here — so
it is the one action guarded three times over:

- the effective role must be ``admin`` (every endpoint tagged ``admin``);
- **and** the caller must administer the media server itself: a promoted Tindeerr admin
  cannot repoint it (``media_server_admin_required``);
- **and** they must have re-authenticated on the **currently configured** media server
  in the last five minutes (``reauth_required``). Credentials are never sent to the new
  address before it is saved.

If the saved server turns out not to be the one Tindeerr was set up with, the same
transaction revokes every session — the caller's included, so this response clears their
cookie — and unlinks every user. Everyone signs in again on the new server, and its
administrators become administrators here as usual.

The other connector kinds arrive in step 3 and answer ``404`` until then.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Response, status

from tindeerr.api.cookies import clear_session_cookie
from tindeerr.api.deps import Services
from tindeerr.api.security import AdminSession, Transport
from tindeerr.api.v1.models import (
    ConnectorResponse,
    ConnectorStatusResponse,
    MediaServerConfigInput,
    SecretStateResponse,
)
from tindeerr.auth import access
from tindeerr.auth.mediaserver import MediaServerInput, MediaServerSettings
from tindeerr.core.errors import ProblemError

router = APIRouter(prefix="/admin", tags=["admin", "console"])

type ConnectorKind = Literal["media_server", "requests", "tmdb", "omdb", "llm"]
#: How many characters of a stored secret the console may show, to tell keys apart.
_LAST4 = 4


def media_server_only(
    kind: Annotated[ConnectorKind, Path(description="Which connector.")],
) -> ConnectorKind:
    """Refuse every connector kind step 2 does not implement yet."""
    if kind != "media_server":
        raise ProblemError(
            status.HTTP_404_NOT_FOUND, "not_found", "This connector arrives in a later step."
        )
    return kind


ConnectorPath = Annotated[ConnectorKind, Depends(media_server_only)]


def as_input(payload: MediaServerConfigInput) -> MediaServerInput:
    """Turn the contract body into the values the connector works with."""
    return MediaServerInput(
        kind=payload.server_type,
        url=payload.url,
        api_key=payload.api_key,
        plex_pin_id=payload.plex_pin_id,
        verify_tls=payload.verify_tls,
        given=frozenset(payload.model_fields_set),
    )


def connector_body(
    settings: MediaServerSettings, status_body: ConnectorStatusResponse, locked: list[str]
) -> ConnectorResponse:
    """Describe the saved connector, with the secret masked (the security model, §7)."""
    return ConnectorResponse(
        kind="media_server",
        configured=True,
        provider=settings.kind,
        url=settings.url,
        verify_tls=settings.verify_tls,
        secret=SecretStateResponse(
            set=bool(settings.secret),
            last4=settings.secret[-_LAST4:] or None,
            locked="api_key" in locked,
        ),
        locked_fields=locked,
        status=status_body,
    )


@router.put(
    "/connectors/{kind}",
    operation_id="saveConnector",
    summary="Configure a connector",
)
async def save_connector(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: MediaServerConfigInput,
    kind: ConnectorPath,
    response: Response,
    services: Services,
    transport: Transport,
    session: AdminSession,
) -> ConnectorResponse:
    """Test and save the media server, applying the identity-change rules."""
    access.require_media_server_admin(session.signed_in_user)
    access.require_recent_reauth(session.session, services.clock.now())
    services.limits.connection_tests.hit(session.session.id)
    saved = await services.connector.save(
        as_input(payload),
        session_id=session.session.id,
        install_id=services.install_id,
        relink=True,
    )
    if saved.identity_changed:
        # Every session went with the old server, this one too.
        clear_session_cookie(response, transport)
    settings = await services.connector.configured()
    body = ConnectorStatusResponse.of(saved.check, services.clock.now())
    if settings is None:  # pragma: no cover - the save just wrote it
        raise ProblemError(500, "internal_error")
    return connector_body(settings, body, services.connector.locked_fields())


@router.post(
    "/connectors/{kind}/test",
    operation_id="testConnector",
    summary="Test a connector without saving",
)
async def test_connector(
    payload: MediaServerConfigInput,
    kind: ConnectorPath,
    services: Services,
    session: AdminSession,
) -> ConnectorStatusResponse:
    """Report a coarse health for the given settings; nothing is saved or consumed."""
    services.limits.connection_tests.hit(session.session.id)
    check = await services.connector.check(
        as_input(payload), session_id=session.session.id, install_id=services.install_id
    )
    return ConnectorStatusResponse.of(check, services.clock.now())
