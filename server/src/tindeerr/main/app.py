"""FastAPI application factory and startup sequence."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr import __version__
from tindeerr.api import health, v1
from tindeerr.api.deps import AppServices
from tindeerr.api.errors import UnhandledErrorMiddleware, install_error_handlers
from tindeerr.api.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from tindeerr.api.proxy import TrustedProxyMiddleware
from tindeerr.core.config import ServerConfig
from tindeerr.core.crypto import SecretCipher
from tindeerr.core.keys import load_key_material
from tindeerr.core.logs import register_secret
from tindeerr.storage.db import create_async_db_engine, database_path
from tindeerr.storage.migrate import upgrade_database
from tindeerr.storage.server_state import ServerStateRepository
from tindeerr.storage.settings import SettingsStore, environment_overrides

BACKUPS_DIR_NAME = "backups"
DOCS_URL = "/api/docs"
OPENAPI_URL = "/api/openapi.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    """Resources opened at startup and closed at shutdown."""

    engine: AsyncEngine
    services: AppServices


async def start(config: ServerConfig) -> Runtime:
    """Load the key, migrate the database and build the services."""
    data_dir = config.data_dir
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    secret_key = config.secret_key.get_secret_value() if config.secret_key else None
    if secret_key is not None:
        register_secret(secret_key)
    keys = load_key_material(secret_key, data_dir)
    register_secret(keys.secret.decode())
    logger.info("secret key loaded", extra={"source": keys.source, "key_id": keys.key_id})

    db_path = database_path(data_dir)
    await asyncio.to_thread(
        upgrade_database, db_path, data_dir / BACKUPS_DIR_NAME, config.db_backups_keep
    )

    engine = create_async_db_engine(db_path)
    settings = SettingsStore(engine, SecretCipher.for_settings(keys), environment_overrides())
    services = AppServices(settings=settings, server_state=ServerStateRepository(engine))
    return Runtime(engine=engine, services=services)


def create_app(config: ServerConfig) -> FastAPI:
    """Build the application. Startup work happens in the lifespan, not here."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        runtime = await start(config)
        app.state.services = runtime.services
        logger.info("tindeerr started", extra={"version": __version__})
        try:
            yield
        finally:
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
    app.add_middleware(SecurityHeadersMiddleware, csp_exempt_paths={DOCS_URL})
    app.add_middleware(RequestIdMiddleware)
    # Outermost: X-Forwarded-For/-Proto are only honoured from these peers; unset, never.
    app.add_middleware(TrustedProxyMiddleware, trusted_proxies=config.trusted_proxies)
    return app
