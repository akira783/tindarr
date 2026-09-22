"""FastAPI application factory and startup sequence."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr import __version__
from tindeerr.adapters.factory import media_server_factory
from tindeerr.adapters.plextv import PlexTvClient
from tindeerr.api import health, v1
from tindeerr.api.context import AllowedHostMiddleware, HostPolicy, RequestContextMiddleware
from tindeerr.api.deps import AppServices
from tindeerr.api.errors import UnhandledErrorMiddleware, install_error_handlers
from tindeerr.api.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from tindeerr.auth.brokered import PlexPinFlow, QuickConnectFlow
from tindeerr.auth.handles import HandleRegistry
from tindeerr.auth.mediaserver import MediaServerConnector
from tindeerr.auth.ratelimit import RateLimits
from tindeerr.auth.sessions import SessionService
from tindeerr.auth.setup import SetupService
from tindeerr.auth.signin import SignInService
from tindeerr.auth.sync import UserSync
from tindeerr.auth.tokens import AccessTokens
from tindeerr.core.clock import Clock, SystemClock
from tindeerr.core.config import ServerConfig
from tindeerr.core.crypto import SecretCipher
from tindeerr.core.keys import KeyPurpose, load_key_material
from tindeerr.core.logs import register_secret
from tindeerr.jobs.media_server import (
    handle_sweep_job,
    quick_connect_probe_job,
    user_sync_job,
)
from tindeerr.jobs.purge import PeriodicJob, purge_job
from tindeerr.ports.media_server import MediaServerFactory
from tindeerr.ports.plextv import PlexTv
from tindeerr.storage.db import create_async_db_engine, database_path
from tindeerr.storage.migrate import upgrade_database
from tindeerr.storage.server_state import ServerStateRepository
from tindeerr.storage.settings import SettingsStore, environment_overrides

BACKUPS_DIR_NAME = "backups"
DOCS_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Wiring:
    """What the composition root injects. Tests replace the parts they need."""

    clock: Clock = field(default_factory=SystemClock)
    #: Builds the media server adapter for a stored connector.
    media_servers: MediaServerFactory | None = None
    #: The plex.tv client every Plex PIN and the Plex adapter go through.
    plex_tv: PlexTv | None = None


@dataclass(frozen=True)
class Runtime:
    """Resources opened at startup and closed at shutdown."""

    engine: AsyncEngine
    services: AppServices
    jobs: tuple[PeriodicJob, ...] = ()


async def start(
    config: ServerConfig, wiring: Wiring | None = None, hosts: HostPolicy | None = None
) -> Runtime:
    """Load the key, migrate the database, build the services and the setup code.

    ``hosts`` is the policy the request middleware already holds, so the host of
    ``public_url`` starts being accepted as soon as it is read.
    """
    wiring = wiring or Wiring()
    data_dir = config.data_dir
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    secret_key = config.secret_key.get_secret_value() if config.secret_key else None
    if secret_key is not None:
        register_secret(secret_key)
    keys = load_key_material(secret_key, data_dir)
    register_secret(keys.secret.decode())
    logger.info("secret key loaded", extra={"source": keys.source, "key_id": keys.key_id})
    if config.allow_http_console:
        logger.warning(
            "TINDEERR_ALLOW_HTTP_CONSOLE is on: console sessions are accepted over plain "
            "HTTP from private addresses, without Secure cookies"
        )

    db_path = database_path(data_dir)
    await asyncio.to_thread(
        upgrade_database, db_path, data_dir / BACKUPS_DIR_NAME, config.db_backups_keep
    )

    engine = create_async_db_engine(db_path)
    settings = SettingsStore(engine, SecretCipher.for_settings(keys), environment_overrides())
    server_state = ServerStateRepository(engine)
    state = await server_state.read()
    sessions = SessionService(
        engine,
        wiring.clock,
        AccessTokens(
            keys.derive(KeyPurpose.JWT_SIGNING), keys.key_id, state.install_id, wiring.clock
        ),
    )
    limits = RateLimits(wiring.clock)
    # The handle registry is also what hands the Plex owner token to the connector, so
    # it is built before it (docs/auth.md, sections 5 and 6).
    handles = HandleRegistry(wiring.clock, limits.handle_creation)
    plex_tv = wiring.plex_tv or PlexTvClient()
    connector = MediaServerConnector(
        engine,
        settings,
        wiring.media_servers or media_server_factory(plex_tv),
        handles,
        wiring.clock,
    )
    setup = SetupService(engine, data_dir, wiring.clock, sessions, connector, settings, limits)
    sign_in = SignInService(
        engine,
        wiring.clock,
        sessions,
        connector,
        settings,
        limits,
        setup,
        server_state,
        state.install_id,
    )
    setup.with_quick_connect(lambda: sign_in.quick_connect.cached)
    plex_pins = PlexPinFlow(sign_in, handles, plex_tv, server_state, state.install_id)
    quick_connect = QuickConnectFlow(sign_in, handles)
    hosts = hosts or HostPolicy(config.allowed_hosts)
    public_url = (await settings.get("public_url")).value
    hosts.set_public_url(public_url if isinstance(public_url, str) else None)

    await setup.ensure_setup_code()

    services = AppServices(
        config=config,
        clock=wiring.clock,
        engine=engine,
        settings=settings,
        server_state=server_state,
        sessions=sessions,
        setup=setup,
        connector=connector,
        sign_in=sign_in,
        plex_pins=plex_pins,
        quick_connect=quick_connect,
        handles=handles,
        limits=limits,
        hosts=hosts,
        install_id=state.install_id,
    )
    jobs = (
        purge_job(sessions),
        user_sync_job(UserSync(engine, connector, wiring.clock, state.install_id)),
        handle_sweep_job(quick_connect),
        quick_connect_probe_job(quick_connect),
    )
    return Runtime(engine=engine, services=services, jobs=jobs)


def create_app(config: ServerConfig, wiring: Wiring | None = None) -> FastAPI:
    """Build the application. Startup work happens in the lifespan, not here."""
    hosts = HostPolicy(config.allowed_hosts)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        runtime = await start(config, wiring, hosts)
        app.state.services = runtime.services
        for job in runtime.jobs:
            job.start()
        logger.info("tindeerr started", extra={"version": __version__})
        try:
            yield
        finally:
            for job in runtime.jobs:
                await job.stop()
            await runtime.engine.dispose()

    app = FastAPI(
        title="Tindeerr API",
        version=__version__,
        lifespan=lifespan,
        docs_url=DOCS_URL if config.api_docs else None,
        redoc_url=None,
        openapi_url=OPENAPI_URL if config.api_docs else None,
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1.router)

    # Starlette wraps in reverse order: the last middleware added is the outermost.
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(AllowedHostMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, csp_exempt_paths={DOCS_URL}, hsts=config.hsts)
    app.add_middleware(RequestIdMiddleware)
    # Outermost: the client address, scheme, host and origin every layer inside reads.
    app.add_middleware(
        RequestContextMiddleware, trusted_proxies=config.trusted_proxies, hosts=hosts
    )
    return app
