"""Services the routes depend on, built by ``tindeerr.main`` at startup."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.api.context import HostPolicy
from tindeerr.auth.brokered import PlexPinFlow, QuickConnectFlow
from tindeerr.auth.handles import HandleRegistry
from tindeerr.auth.mediaserver import MediaServerConnector
from tindeerr.auth.ratelimit import RateLimits
from tindeerr.auth.sessions import SessionService
from tindeerr.auth.setup import SetupService
from tindeerr.auth.signin import SignInService
from tindeerr.core.clock import Clock
from tindeerr.core.config import ServerConfig
from tindeerr.storage.server_state import ServerStateRepository
from tindeerr.storage.settings import SettingsStore


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
    #: Every sign-in method, and the two brokered flows behind their handles.
    sign_in: SignInService
    plex_pins: PlexPinFlow
    quick_connect: QuickConnectFlow
    #: The in-memory registry both brokered flows share (docs/auth.md, section 5).
    handles: HandleRegistry
    limits: RateLimits
    hosts: HostPolicy
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
