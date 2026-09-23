"""FastAPI application factory and startup sequence."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr import __version__
from tindarr.adapters.factory import connector_factories, media_server_factory
from tindarr.adapters.plextv import PlexTvClient
from tindarr.adapters.publicurl import HttpPublicUrlProbe
from tindarr.api import health, v1
from tindarr.api.console import ConsoleMiddleware, WebConsole
from tindarr.api.context import (
    AllowedHostMiddleware,
    HostPolicy,
    RequestContextMiddleware,
    host_of,
)
from tindarr.api.deps import AppServices
from tindarr.api.errors import UnhandledErrorMiddleware, install_error_handlers
from tindarr.api.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from tindarr.auth.brokered import PlexPinFlow, QuickConnectFlow
from tindarr.auth.handles import HandleRegistry
from tindarr.auth.mediaserver import MediaServerConnector
from tindarr.auth.pairing import PairingService
from tindarr.auth.publicurl import PublicUrlVerifier
from tindarr.auth.ratelimit import RateLimits
from tindarr.auth.sessions import SessionService
from tindarr.auth.setup import SetupService
from tindarr.auth.signin import SignInService
from tindarr.auth.sync import UserSync
from tindarr.auth.tokens import AccessTokens
from tindarr.connectors import ConnectorService
from tindarr.core.clock import Clock, SystemClock
from tindarr.core.config import ServerConfig
from tindarr.core.crypto import SecretCipher
from tindarr.core.keys import KeyPurpose, load_key_material
from tindarr.core.logs import register_secret
from tindarr.jobs.media_server import (
    handle_sweep_job,
    quick_connect_probe_job,
    user_sync_job,
)
from tindarr.jobs.publicurl import public_url_check_job
from tindarr.jobs.purge import BackgroundJob, purge_job
from tindarr.ports.factories import ConnectorFactories
from tindarr.ports.media_server import MediaServerFactory
from tindarr.ports.plextv import PlexTv
from tindarr.ports.publicurl import PublicUrlProbe
from tindarr.storage.db import create_async_db_engine, database_path
from tindarr.storage.migrate import upgrade_database
from tindarr.storage.server_state import ServerStateRepository
from tindarr.storage.settings import SettingsStore, environment_overrides

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
    #: How the server calls its own public address to check ``public_url``.
    public_url_probe: PublicUrlProbe | None = None
    #: Builds TMDb, OMDb, the request backend and the AI providers (step 3).
    connectors: ConnectorFactories | None = None


@dataclass(frozen=True)
class Runtime:
    """Resources opened at startup and closed at shutdown."""

    engine: AsyncEngine
    services: AppServices
    jobs: tuple[BackgroundJob, ...] = ()


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
            "TINDARR_ALLOW_HTTP_CONSOLE is on: console sessions are accepted over plain "
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
        wiring.media_servers or media_server_factory(plex_tv, clock=wiring.clock),
        handles,
        wiring.clock,
    )
    connectors = ConnectorService(settings, wiring.connectors or connector_factories())
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
    public_url_setting = await settings.get("public_url")
    public_url = public_url_setting.value if isinstance(public_url_setting.value, str) else None
    hosts.set_public_url(public_url)
    public_url_verifier = PublicUrlVerifier(
        keys.derive(KeyPurpose.PUBLIC_URL_PROOF),
        wiring.public_url_probe or HttpPublicUrlProbe(),
        wiring.clock,
    )

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
        connectors=connectors,
        sign_in=sign_in,
        plex_pins=plex_pins,
        quick_connect=quick_connect,
        handles=handles,
        pairings=PairingService(engine, wiring.clock, sessions),
        limits=limits,
        hosts=hosts,
        install_id=state.install_id,
        public_url=public_url_verifier,
    )
    jobs: tuple[BackgroundJob, ...] = (
        purge_job(sessions),
        user_sync_job(UserSync(engine, connector, wiring.clock, state.install_id)),
        handle_sweep_job(quick_connect),
        quick_connect_probe_job(quick_connect),
    )
    public_url_host = host_of(public_url)
    if public_url is not None and public_url_host is not None and public_url_setting.locked:
        # Only the operator's own value: one set from the console was checked first.
        jobs += (public_url_check_job(public_url_verifier, public_url, public_url_host),)
    return Runtime(engine=engine, services=services, jobs=jobs)


def create_app(config: ServerConfig, wiring: Wiring | None = None) -> FastAPI:
    """Build the application. Startup work happens in the lifespan, not here."""
    hosts = HostPolicy(config.allowed_hosts)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        runtime = await start(config, wiring, hosts)
        app.state.services = runtime.services
        # Kept on the application so an operator (and the tests) can see what is
        # scheduled; the lifespan is the only thing that starts and stops them.
        app.state.jobs = runtime.jobs
        for job in runtime.jobs:
            job.start()
        logger.info("tindarr started", extra={"version": __version__})
        try:
            yield
        finally:
            for job in runtime.jobs:
                await job.stop()
            await runtime.engine.dispose()

    app = FastAPI(
        title="Tindarr API",
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
    # Innermost: the console, so an unknown API path keeps the router's own 404 problem
    # and a wrong method its 405 (tindarr.api.console).
    app.add_middleware(ConsoleMiddleware, console=WebConsole(config.web_dir))
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(AllowedHostMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, csp_exempt_paths={DOCS_URL}, hsts=config.hsts)
    app.add_middleware(RequestIdMiddleware)
    # Outermost: the client address, scheme, host and origin every layer inside reads.
    app.add_middleware(
        RequestContextMiddleware, trusted_proxies=config.trusted_proxies, hosts=hosts
    )
    return app
