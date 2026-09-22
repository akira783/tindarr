"""Command line: ``tindeerr [serve]`` runs the server, ``tindeerr healthcheck`` probes it."""

import argparse
import logging
import urllib.request
from collections.abc import Sequence
from urllib.error import URLError

import uvicorn

from tindeerr import __version__
from tindeerr.core.config import ConfigError, ServerConfig, load_config
from tindeerr.core.logs import configure_logging
from tindeerr.main.app import create_app
from tindeerr.storage.settings import environment_overrides

HEALTHCHECK_TIMEOUT_S = 3
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})  # noqa: S104 - compared, not bound

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tindeerr", description="Tindeerr server.")
    parser.add_argument("--version", action="version", version=f"tindeerr {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "healthcheck"),
        default="serve",
        help="serve (default) runs the server; healthcheck exits 0 when /healthz answers",
    )
    return parser


def serve(config: ServerConfig) -> int:
    """Run the HTTP server until it is stopped."""
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        lifespan="on",
        log_config=None,
        access_log=False,
        proxy_headers=False,  # handled by the app, from TINDEERR_TRUSTED_PROXIES only
        server_header=False,
    )
    return 0


def healthcheck(config: ServerConfig) -> int:
    """Probe ``/healthz`` on the local server; used by the container HEALTHCHECK."""
    host = "127.0.0.1" if config.host in _WILDCARD_HOSTS else config.host
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{config.port}/healthz"
    # Never through HTTP_PROXY / HTTPS_PROXY: the probe targets this very container.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=HEALTHCHECK_TIMEOUT_S) as response:
            return 0 if response.status == 200 else 1  # noqa: PLR2004
    except (URLError, OSError):
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``tindeerr`` command."""
    args = _parser().parse_args(argv)
    try:
        config = load_config()
        if args.command == "serve":
            # Checked again at startup; failing here gives a one-line error, exit code 2.
            environment_overrides()
    except ConfigError as exc:
        configure_logging("INFO")
        logger.error("%s", exc)  # noqa: TRY400 - the traceback adds nothing here
        return 2
    if args.command == "healthcheck":
        return healthcheck(config)
    configure_logging(config.log_level)
    return serve(config)
