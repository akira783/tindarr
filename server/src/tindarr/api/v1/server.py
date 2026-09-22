"""``GET /api/v1/server/info``: public server identity, version and capabilities."""

from typing import Final, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from tindarr import __version__
from tindarr.api.deps import Services
from tindarr.api.security import Context
from tindarr.api.v1.models import MediaServerInfo
from tindarr.auth.methods import AuthMethod, sign_in_methods
from tindarr.ports.media_server import as_media_server_kind
from tindarr.storage.settings import as_password_sign_in

API_VERSION: Final = 1
#: Oldest app release that can talk to this server.
MIN_APP_VERSION: Final = "0.1.0"
TMDB_IMAGE_BASE_URL: Final = "https://image.tmdb.org/t/p/"

router = APIRouter(prefix="/server", tags=["server"])


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
async def get_server_info(services: Services, context: Context) -> ServerInfo:
    """Tell the app what this server is and what it can do.

    Public and rate-limited per client address. The answer depends on the caller's
    network (``password_sign_in = lan_only``), so it is never cached. It never calls the
    media server: whether Quick Connect is on comes from the cache a background probe
    keeps fresh, and the method is left out while that answer is unknown.
    """
    services.limits.public.hit(context.rate_limit_key)
    name = (await services.settings.get("server_name")).value
    kind_value = (await services.settings.get("media_server_kind")).value
    kind = as_media_server_kind(kind_value)
    password_sign_in = as_password_sign_in((await services.settings.get("password_sign_in")).value)
    public_url = (await services.settings.get("public_url")).value
    setup_required = not await services.server_state.setup_completed()
    methods = sign_in_methods(
        kind,
        password_sign_in=password_sign_in,
        client_is_private=context.client_is_private,
        quick_connect_enabled=services.sign_in.quick_connect.cached,
        pairing_available=isinstance(public_url, str),
    )
    return ServerInfo(
        name=name if isinstance(name, str) and name else "Tindarr",
        version=__version__,
        api_version=API_VERSION,
        min_app_version=MIN_APP_VERSION,
        setup_required=setup_required,
        media_server=MediaServerInfo(kind=kind) if kind is not None else None,
        # Nobody signs in before setup completes: the console claims the server first.
        auth_methods=[] if setup_required else methods,
        capabilities=[],
        tmdb_image_base_url=TMDB_IMAGE_BASE_URL,
    )
