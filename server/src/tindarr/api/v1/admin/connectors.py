"""Administration endpoints for the connectors.

Two very different things share these three routes, and the difference is worth keeping
in mind while reading them.

**The media server** (docs/auth.md, section 6) decides who can sign in and who is an
administrator, so repointing it is the most dangerous thing an administrator can do — a
connector aimed at a server the attacker controls collects everyone's password at their
next sign-in and makes its owner an administrator here. It is therefore guarded three
times over: the effective role must be ``admin``; the caller must administer the media
server itself, so a promoted Tindarr admin cannot repoint it
(``media_server_admin_required``); and they must have re-authenticated on the
**currently configured** media server in the last five minutes (``reauth_required``).
Credentials are never sent to the new address before it is saved, and a saved server
that turns out not to be the one Tindarr was set up with costs every session and every
user link in the same transaction.

**The four optional connectors** (TMDb, OMDb, the request backend, the AI provider)
decide nothing about identity. Any administrator may add, test, change or remove them.
What they share with the media server is the rule that a stored secret is never sent to
an address it was not stored for (``secret_required``), which is what stops a stolen
console session from using this server as a courier for the keys it holds.

Neither kind is stored before its connection test passes, and no test answer carries
more than a coarse health value and, at most, the remote product's own name and version
(the security model, section 7).

``GET /admin/connectors`` never calls anything: a configured connector's health is
``unknown`` until an administrator presses "test".
"""

from collections.abc import Mapping
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Body, Path, Response, status

from tindarr.api.cookies import clear_session_cookie
from tindarr.api.deps import Services
from tindarr.api.security import AdminSession, Transport
from tindarr.api.v1.models import (
    ConnectorInputBody,
    ConnectorListResponse,
    ConnectorResponse,
    ConnectorStatusResponse,
    LlmModelsResponse,
    LlmSettingsInput,
    MediaServerConfigInput,
    OptionalConnectorBody,
    SecretStateResponse,
    as_connector_input,
)
from tindarr.auth import access
from tindarr.auth.mediaserver import MediaServerInput, MediaServerSettings
from tindarr.connectors import ConnectorState, OptionalConnectorKind
from tindarr.core.errors import ProblemError

router = APIRouter(tags=["admin", "console"])

type ConnectorKind = Literal["media_server", "requests", "tmdb", "omdb", "llm"]
CONNECTOR_KINDS: tuple[ConnectorKind, ...] = get_args(ConnectorKind.__value__)
#: How many characters of a stored secret the console may show, to tell keys apart.
_LAST4 = 4
#: And how long it has to be before showing them gives anything away.
_MIN_SECRET_TO_HINT = 12

ConnectorPath = Annotated[ConnectorKind, Path(description="Which connector.")]
ConnectorBody = Annotated[ConnectorInputBody, Body()]


def _mismatch(kind: ConnectorKind) -> ProblemError:
    return ProblemError(
        status.HTTP_400_BAD_REQUEST,
        "validation_error",
        f"connector: this body is not a {kind} connector",
    )


def media_server_body(payload: ConnectorInputBody, kind: ConnectorKind) -> MediaServerConfigInput:
    """Return the body as the media server's, or refuse a body of another kind."""
    if kind != "media_server" or not isinstance(payload, MediaServerConfigInput):
        raise _mismatch(kind)
    return payload


def optional_body(payload: ConnectorInputBody, kind: ConnectorKind) -> OptionalConnectorBody:
    """Return the body as an optional connector's, checked against the path."""
    if isinstance(payload, MediaServerConfigInput) or payload.connector != kind:
        raise _mismatch(kind)
    return payload


def as_input(payload: MediaServerConfigInput) -> MediaServerInput:
    """Turn the contract body into the values the media server connector works with."""
    return MediaServerInput(
        kind=payload.server_type,
        url=payload.url,
        api_key=payload.api_key,
        plex_pin_id=payload.plex_pin_id,
        verify_tls=payload.verify_tls,
        given=frozenset(payload.model_fields_set),
    )


def secret_state(secret: str, *, locked: bool, show_last4: bool = True) -> SecretStateResponse:
    """Say whether the secret is set, and show its last characters where that helps.

    Four characters are what lets an administrator tell "the key I just made" from the
    old one, and they are shown only when there is plenty left unshown: slicing a short
    string returns all of it. A Plex connector's secret is masked entirely — it is not a
    key but the **account token** of the server's owner, of which there is only ever
    one, so those four characters would identify nothing and give away part of a
    credential that opens a whole Plex account.
    """
    shown = show_last4 and len(secret) >= _MIN_SECRET_TO_HINT
    return SecretStateResponse(
        set=bool(secret), last4=secret[-_LAST4:] if shown else None, locked=locked
    )


def connector_body(
    settings: MediaServerSettings, status_body: ConnectorStatusResponse, locked: list[str]
) -> ConnectorResponse:
    """Describe the saved media server connector, with the secret masked."""
    return ConnectorResponse(
        kind="media_server",
        configured=True,
        provider=settings.kind,
        url=settings.url,
        verify_tls=settings.verify_tls,
        secret=secret_state(
            settings.secret, locked="api_key" in locked, show_last4=settings.kind != "plex"
        ),
        locked_fields=locked,
        status=status_body,
    )


def optional_connector_body(
    kind: OptionalConnectorKind,
    state: ConnectorState | None,
    status_body: ConnectorStatusResponse,
    locked: list[str],
    forced: Mapping[str, object],
) -> ConnectorResponse:
    """Describe an optional connector, configured or not, with the secret masked.

    ``forced`` carries the non-secret values an environment variable sets, and they are
    shown even when nothing is stored yet. That matters: an operator who pins the
    request backend's address but leaves its key to the administrator would otherwise
    hand them a console with an empty, disabled address field and no way to finish.
    """
    return ConnectorResponse(
        kind=kind,
        configured=state is not None,
        provider=_forced_text(forced, "provider") or (state.provider if state else None),
        url=_forced_text(forced, "url", "base_url") or (state.url if state else None),
        verify_tls=_forced_flag(forced) if "verify_tls" in forced else _verify_tls(state),
        model=state.model if state is not None else None,
        tv_seasons=_forced_seasons(forced) or (state.tv_seasons if state else None),
        secret=secret_state(state.secret if state is not None else "", locked="api_key" in locked),
        locked_fields=locked,
        status=status_body,
    )


def _verify_tls(state: ConnectorState | None) -> bool | None:
    return state.verify_tls if state is not None else None


def _forced_text(forced: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = forced.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _forced_flag(forced: Mapping[str, object]) -> bool | None:
    value = forced.get("verify_tls")
    return value if isinstance(value, bool) else None


def _forced_seasons(forced: Mapping[str, object]) -> Literal["all", "first"] | None:
    value = forced.get("tv_seasons")
    if value == "first":
        return "first"
    return "all" if value == "all" else None


def unconfigured_media_server(locked: list[str]) -> ConnectorResponse:
    """Describe a media server connector nothing has been stored for."""
    return ConnectorResponse(
        kind="media_server",
        configured=False,
        secret=SecretStateResponse(set=False, locked="api_key" in locked),
        locked_fields=locked,
        status=ConnectorStatusResponse.untested(configured=False),
    )


async def _listed(services: Services, kind: ConnectorKind) -> ConnectorResponse:
    if kind == "media_server":
        settings = await services.connector.configured()
        locked = services.connector.locked_fields()
        if settings is None:
            return unconfigured_media_server(locked)
        return connector_body(settings, ConnectorStatusResponse.untested(configured=True), locked)
    optional: OptionalConnectorKind = kind
    state = await services.connectors.state(optional)
    return optional_connector_body(
        optional,
        state,
        ConnectorStatusResponse.untested(configured=state is not None),
        services.connectors.locked_fields(optional),
        services.connectors.locked_values(optional),
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
    return ConnectorListResponse(
        connectors=[await _listed(services, kind) for kind in CONNECTOR_KINDS]
    )


@router.delete(
    "/connectors/{kind}",
    operation_id="deleteConnector",
    summary="Remove an optional connector (omdb, requests)",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connector(kind: ConnectorPath, services: Services, session: AdminSession) -> None:
    """Forget an optional connector. The media server is not one of them."""
    if kind == "media_server":
        raise ProblemError(
            status.HTTP_409_CONFLICT,
            "media_server_required",
            "Tindarr cannot sign anyone in without a media server; point it at another "
            "one instead of removing it.",
        )
    await services.connectors.remove(kind)


@router.put(
    "/connectors/{kind}",
    operation_id="saveConnector",
    summary="Configure a connector",
)
async def save_connector(  # noqa: PLR0913, PLR0917 - one per dependency
    payload: ConnectorBody,
    kind: ConnectorPath,
    response: Response,
    services: Services,
    transport: Transport,
    session: AdminSession,
) -> ConnectorResponse:
    """Test the connector and save it; nothing is stored when the test fails."""
    services.limits.connection_tests.hit(session.session.id)
    if kind != "media_server":
        request = as_connector_input(optional_body(payload, kind))
        check = await services.connectors.save(request)
        return optional_connector_body(
            request.kind,
            await services.connectors.state(request.kind),
            ConnectorStatusResponse.of(check, services.clock.now()),
            services.connectors.locked_fields(request.kind),
            services.connectors.locked_values(request.kind),
        )
    access.require_media_server_admin(session.signed_in_user)
    access.require_recent_reauth(session.session, services.clock.now())
    saved = await services.connector.save(
        as_input(media_server_body(payload, kind)),
        session_id=session.session.id,
        install_id=services.install_id,
        relink=True,
    )
    if saved.identity_changed:
        # Every session went with the old server, this one too.
        clear_session_cookie(response, transport)
    settings = await services.connector.configured()
    if settings is None:  # pragma: no cover - the save just wrote it
        raise ProblemError(500, "internal_error")
    return connector_body(
        settings,
        ConnectorStatusResponse.of(saved.check, services.clock.now()),
        services.connector.locked_fields(),
    )


@router.post(
    "/connectors/{kind}/test",
    operation_id="testConnector",
    summary="Test a connector without saving",
)
async def test_connector(
    payload: ConnectorBody, kind: ConnectorPath, services: Services, session: AdminSession
) -> ConnectorStatusResponse:
    """Report a coarse health for the given settings; nothing is saved or consumed."""
    services.limits.connection_tests.hit(session.session.id)
    if kind == "media_server":
        check = await services.connector.check(
            as_input(media_server_body(payload, kind)),
            session_id=session.session.id,
            install_id=services.install_id,
        )
    else:
        check = await services.connectors.check(as_connector_input(optional_body(payload, kind)))
    return ConnectorStatusResponse.of(check, services.clock.now())


@router.post(
    "/llm/models",
    operation_id="listLlmModels",
    summary="List the models a provider offers",
)
async def list_llm_models(
    payload: Annotated[LlmSettingsInput, Body()], services: Services, session: AdminSession
) -> LlmModelsResponse:
    """Ask the provider which models it serves, so no model id is ever hard-coded.

    Only ids are returned, parsed from the provider's expected shape: like a connection
    test, this must not become a way to read something else off an address the caller
    chose (the security model, section 7).
    """
    services.limits.connection_tests.hit(session.session.id)
    models = await services.connectors.list_models(as_connector_input(payload))
    return LlmModelsResponse(models=models)
