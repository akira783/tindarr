"""Administration endpoints. Step 2b implements the media server connector.

docs/auth.md, section 6. Repointing the media server is the most dangerous thing an
administrator can do — a connector aimed at a server the attacker controls collects
everyone's password at their next sign-in and makes its owner an administrator here — so
it is the one action guarded three times over:

- the effective role must be ``admin`` (every endpoint tagged ``admin``);
- **and** the caller must administer the media server itself: a promoted Tindarr admin
  cannot repoint it (``media_server_admin_required``);
- **and** they must have re-authenticated on the **currently configured** media server
  in the last five minutes (``reauth_required``). Credentials are never sent to the new
  address before it is saved.

If the saved server turns out not to be the one Tindarr was set up with, the same
transaction revokes every session — the caller's included, so this response clears their
cookie — and unlinks every user. Everyone signs in again on the new server, and its
administrators become administrators here as usual.

``GET /admin/connectors`` lists every kind the contract knows, so the console can show
the page it will keep in steps 3 and 4; only ``media_server`` is configured in step 2
and the others answer ``404`` to every other call until then. Listing never calls a
remote service: a configured connector's health is ``unknown`` until the administrator
presses "test".

The media server connector cannot be **removed**: a Tindarr without one signs nobody
in. It is repointed with ``PUT``, under the three guards above.
"""

from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, Path, Response, status

from tindarr.api.cookies import clear_session_cookie
from tindarr.api.deps import Services
from tindarr.api.security import AdminSession, Transport
from tindarr.api.v1.models import (
    ConnectorListResponse,
    ConnectorResponse,
    ConnectorStatusResponse,
    MediaServerConfigInput,
    SecretStateResponse,
)
from tindarr.auth import access
from tindarr.auth.mediaserver import MediaServerInput, MediaServerSettings
from tindarr.core.errors import ProblemError

router = APIRouter(tags=["admin", "console"])

type ConnectorKind = Literal["media_server", "requests", "tmdb", "omdb", "llm"]
CONNECTOR_KINDS: tuple[ConnectorKind, ...] = get_args(ConnectorKind.__value__)
#: How many characters of a stored secret the console may show, to tell keys apart.
_LAST4 = 4


def _not_implemented() -> ProblemError:
    return ProblemError(
        status.HTTP_404_NOT_FOUND, "not_found", "This connector arrives in a later step."
    )


def media_server_only(
    kind: Annotated[ConnectorKind, Path(description="Which connector.")],
) -> ConnectorKind:
    """Refuse every connector kind step 2 does not implement yet."""
    if kind != "media_server":
        raise _not_implemented()
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


def secret_state(settings: MediaServerSettings, locked: list[str]) -> SecretStateResponse:
    """Say whether the secret is set, and show its last characters where that helps.

    An API key is one of several an administrator may hold, and four characters are
    what lets them tell "the key I just made" from "the old one". A Plex connector's
    secret is not a key but the **account token** of the server's owner: there is only
    ever one, so those four characters would identify nothing and only give away part
    of a credential that opens the whole Plex account. It is masked entirely.
    """
    plex = settings.kind == "plex"
    return SecretStateResponse(
        set=bool(settings.secret),
        last4=None if plex else settings.secret[-_LAST4:] or None,
        locked="api_key" in locked,
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
        secret=secret_state(settings, locked),
        locked_fields=locked,
        status=status_body,
    )


def unconfigured(kind: ConnectorKind) -> ConnectorResponse:
    """Describe a connector nothing has been stored for."""
    return ConnectorResponse(
        kind=kind,
        configured=False,
        secret=SecretStateResponse(set=False),
        locked_fields=[],
        status=ConnectorStatusResponse.untested(configured=False),
    )


@router.get(
    "/connectors",
    operation_id="listConnectors",
    summary="All connectors, secrets masked",
)
async def list_connectors(services: Services, session: AdminSession) -> ConnectorListResponse:
    """List every connector kind, without calling any of them.

    A configured connector's health is ``unknown``: finding out costs a network call
    per connector, and the console asks for it explicitly with ``…/test``.
    """
    settings = await services.connector.configured()
    locked = services.connector.locked_fields()
    connectors = [
        unconfigured(kind)
        if kind != "media_server" or settings is None
        else connector_body(settings, ConnectorStatusResponse.untested(configured=True), locked)
        for kind in CONNECTOR_KINDS
    ]
    return ConnectorListResponse(connectors=connectors)


@router.delete(
    "/connectors/{kind}",
    operation_id="deleteConnector",
    summary="Remove an optional connector (omdb, requests)",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connector(
    kind: Annotated[ConnectorKind, Path(description="Which connector.")],
    services: Services,
    session: AdminSession,
) -> None:
    """Refuse, in step 2: the media server is required and the rest do not exist yet."""
    if kind != "media_server":
        raise _not_implemented()
    raise ProblemError(
        status.HTTP_409_CONFLICT,
        "media_server_required",
        "Tindarr cannot sign anyone in without a media server; point it at another "
        "one instead of removing it.",
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
