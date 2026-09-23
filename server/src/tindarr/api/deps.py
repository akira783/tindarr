"""Services the routes depend on, built by ``tindarr.main`` at startup."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.api.context import HostPolicy
from tindarr.auth.brokered import PlexPinFlow, QuickConnectFlow
from tindarr.auth.handles import HandleRegistry
from tindarr.auth.mediaserver import MediaServerConnector
from tindarr.auth.pairing import PairingService
from tindarr.auth.publicurl import PublicUrlVerifier
from tindarr.auth.ratelimit import RateLimits
from tindarr.auth.sessions import SessionService
from tindarr.auth.setup import SetupService
from tindarr.auth.signin import SignInService
from tindarr.connectors import ConnectorService
from tindarr.core.clock import Clock
from tindarr.core.config import ServerConfig
from tindarr.storage.server_state import ServerStateRepository
from tindarr.storage.settings import SettingsStore


@dataclass(frozen=True)
class AppServices:
    """Everything a route may need, stored in ``app.state.services`` during the lifespan."""

    config: ServerConfig
    clock: Clock
    #: The database, for the repositories the routes compose in one transaction.
    engine: AsyncEngine
    settings: SettingsStore
    server_state: ServerStateRepository
    sessions: SessionService
    setup: SetupService
    connector: MediaServerConnector
    #: TMDb, OMDb, the request backend and the AI provider (step 3).
    connectors: ConnectorService
    #: Every sign-in method, and the two brokered flows behind their handles.
    sign_in: SignInService
    plex_pins: PlexPinFlow
    quick_connect: QuickConnectFlow
    #: The in-memory registry both brokered flows share (docs/auth.md, section 5).
    handles: HandleRegistry
    #: Connecting a phone from the console (docs/auth.md, section 9).
    pairings: PairingService
    limits: RateLimits
    hosts: HostPolicy
    #: Checks that ``public_url`` really reaches this server (docs/auth.md, section 10).
    public_url: PublicUrlVerifier
    #: Random per database: the JWT issuer and the media server ``DeviceId``.
    install_id: str


def get_services(request: Request) -> AppServices:
    """FastAPI dependency returning the application's services."""
    services: object = getattr(request.app.state, "services", None)
    if not isinstance(services, AppServices):  # pragma: no cover - wiring error
        msg = "application services are not initialised"
        raise TypeError(msg)
    return services


Services = Annotated[AppServices, Depends(get_services)]
