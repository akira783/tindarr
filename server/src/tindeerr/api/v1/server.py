"""``GET /api/v1/server/info``: public server identity, version and capabilities."""

from typing import Final, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from tindeerr import __version__
from tindeerr.api.deps import Services
from tindeerr.auth.methods import AuthMethod, sign_in_methods
from tindeerr.ports.media_server import MediaServerKind, as_media_server_kind

API_VERSION: Final = 1
#: Oldest app release that can talk to this server.
MIN_APP_VERSION: Final = "0.1.0"
TMDB_IMAGE_BASE_URL: Final = "https://image.tmdb.org/t/p/"

router = APIRouter(prefix="/server", tags=["server"])


class MediaServerInfo(BaseModel):
    """The configured media server."""

    kind: MediaServerKind


class ServerInfo(BaseModel):
    """Contract schema ``ServerInfo``. Public: no user data, no internal URL."""

    name: str
    version: str
    api_version: Literal[1]
    min_app_version: str
    setup_required: bool
    media_server: MediaServerInfo | None
    auth_methods: list[AuthMethod]
    capabilities: list[str]
    tmdb_image_base_url: str


@router.get(
    "/info",
    operation_id="getServerInfo",
    summary="Server identity, version and capabilities",
)
async def get_server_info(services: Services) -> ServerInfo:
    """Tell the app what this server is and what it can do."""
    name = (await services.settings.get("server_name")).value
    kind_value = (await services.settings.get("media_server_kind")).value
    kind = as_media_server_kind(kind_value)
    return ServerInfo(
        name=name if isinstance(name, str) and name else "Tindeerr",
        version=__version__,
        api_version=API_VERSION,
        min_app_version=MIN_APP_VERSION,
        setup_required=not await services.server_state.setup_completed(),
        media_server=MediaServerInfo(kind=kind) if kind is not None else None,
        auth_methods=sign_in_methods(kind),
        capabilities=[],
        tmdb_image_base_url=TMDB_IMAGE_BASE_URL,
    )
