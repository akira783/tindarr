"""Services the routes depend on, built by ``tindeerr.main`` at startup."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from tindeerr.storage.server_state import ServerStateRepository
from tindeerr.storage.settings import SettingsStore


@dataclass(frozen=True)
class AppServices:
    """Everything a route may need, stored in ``app.state.services`` during the lifespan."""

    settings: SettingsStore
    server_state: ServerStateRepository


def get_services(request: Request) -> AppServices:
    """FastAPI dependency returning the application's services."""
    services: object = getattr(request.app.state, "services", None)
    if not isinstance(services, AppServices):  # pragma: no cover - wiring error
        msg = "application services are not initialised"
        raise TypeError(msg)
    return services


Services = Annotated[AppServices, Depends(get_services)]
