"""Command line.

``tindarr [serve]`` runs the server, ``tindarr healthcheck`` probes it, and
``tindarr media-server reset`` is the host-side recovery path when the media server was
replaced and nobody can re-authenticate on it any more (docs/auth.md, section 3).
"""

import argparse
import asyncio
import logging
import sys
import urllib.request
from collections.abc import Sequence
from urllib.error import URLError

import uvicorn

from tindarr import __version__
from tindarr.core.config import ConfigError, ServerConfig, load_config
from tindarr.core.logs import configure_logging
from tindarr.main.app import create_app, start
from tindarr.storage.settings import environment_overrides

HEALTHCHECK_TIMEOUT_S = 3
#: Connections uvicorn serves at the same time; beyond it a connection gets `503` and
#: is closed rather than queued. A household needs a handful; the number is there so a
#: flood of requests the application deliberately holds open (the sign-in pause of
#: docs/auth.md, section 8) cannot accumulate sockets and tasks without end.
MAX_CONCURRENCY = 256
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})  # noqa: S104 - compared, not bound
RESET_CONFIRMATION = "reset"

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tindarr", description="Tindarr server.")
    parser.add_argument("--version", action="version", version=f"tindarr {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the server (the default)")
    commands.add_parser("healthcheck", help="exit 0 when /healthz answers")
    media_server = commands.add_parser("media-server", help="media server connector maintenance")
    actions = media_server.add_subparsers(dest="action", required=True)
    reset = actions.add_parser(
        "reset",
        help="clear the media server, sign everyone out and start first-run setup again",
    )
    reset.add_argument("--yes", action="store_true", help="do not ask for confirmation (scripts)")
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
        proxy_headers=False,  # handled by the app, from TINDARR_TRUSTED_PROXIES only
        server_header=False,
        limit_concurrency=MAX_CONCURRENCY,
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


def reset_media_server(config: ServerConfig, *, confirmed: bool) -> int:
    """Clear the media server connector and start first-run setup again.

    Everyone is signed out and every user is unlinked: their data stays, but they sign
    in again on the new server, and the first administrator to do so completes setup.
    """
    if not confirmed and not _confirmed_on_the_terminal():
        logger.error(
            "nothing was changed. Run 'tindarr media-server reset --yes' to confirm: it "
            "clears the media server, signs everyone out and unlinks every user"
        )
        return 2

    async def run() -> int:
        runtime = await start(config)
        try:
            path = await runtime.services.setup.reset()
        finally:
            await runtime.engine.dispose()
        logger.info("media server cleared: set the server up again with the code in %s", path)
        return 0

    return asyncio.run(run())


def _confirmed_on_the_terminal() -> bool:
    if not sys.stdin.isatty():  # pragma: no cover - only reached from a terminal
        return False
    answer = input(  # pragma: no cover - interactive
        "This clears the media server, signs everyone out and unlinks every user.\n"
        f"Type '{RESET_CONFIRMATION}' to confirm: "
    )
    return answer.strip() == RESET_CONFIRMATION  # pragma: no cover - interactive


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``tindarr`` command."""
    args = _parser().parse_args(argv)
    command = args.command or "serve"
    try:
        config = load_config()
        if command != "healthcheck":
            # Checked again at startup; failing here gives a one-line error, exit code 2.
            environment_overrides()
    except ConfigError as exc:
        configure_logging("INFO")
        logger.error("%s", exc)  # noqa: TRY400 - the traceback adds nothing here
        return 2
    if command == "healthcheck":
        return healthcheck(config)
    configure_logging(config.log_level)
    if command == "media-server":
        return reset_media_server(config, confirmed=bool(args.yes))
    return serve(config)
