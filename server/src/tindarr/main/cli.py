"""Command line.

``tindarr [serve]`` runs the server, ``tindarr healthcheck`` probes it, and
``tindarr media-server reset`` is the host-side recovery path when the media server was
replaced and nobody can re-authenticate on it any more (docs/auth.md, section 3).

``tindarr eval`` is the offline evaluation harness of ADR 0013 (docs/evaluation.md). It
is a development command: it opens no instance database, it makes no network call unless
``--live`` is given and confirmed, and it exits 1 — not 2 — when the numbers regressed,
so a build can tell "worse" from "broken".
"""

import argparse
import asyncio
import logging
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from urllib.error import URLError

import uvicorn

from tindarr import __version__
from tindarr.core.config import ConfigError, ServerConfig, load_config
from tindarr.core.logs import configure_logging
from tindarr.main.app import create_app, start
from tindarr.main.evaluation import (
    DEFAULT_FIXTURES,
    STRATEGIES,
    EvalError,
    EvalPaths,
    compare_to_baseline,
    import_fixture,
    run_evaluation,
    write_baseline,
    write_fixtures,
    write_report,
)
from tindarr.storage.settings import environment_overrides
from tindarr.swipe.evaluation import ReplayOptions

HEALTHCHECK_TIMEOUT_S = 3
#: Connections uvicorn serves at the same time; beyond it a connection gets `503` and
#: is closed rather than queued. A household needs a handful; the number is there so a
#: flood of requests the application deliberately holds open (the sign-in pause of
#: docs/auth.md, section 8) cannot accumulate sockets and tasks without end.
MAX_CONCURRENCY = 256
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})  # noqa: S104 - compared, not bound
RESET_CONFIRMATION = "reset"
#: The seed the committed fixture was generated from.
SYNTHETIC_SEED = 20260923

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
    _add_eval(commands.add_parser("eval", help="offline evaluation harness (development)"))
    return parser


def _add_eval(harness: argparse.ArgumentParser) -> None:
    """Add the actions of ``tindarr eval`` (docs/evaluation.md)."""
    actions = harness.add_subparsers(dest="action", required=True)

    run = actions.add_parser("run", help="replay a vote set past a strategy and print the table")
    run.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="fixture directory")
    run.add_argument("--strategy", default="popular", choices=sorted(STRATEGIES))
    run.add_argument("--batch-size", type=int, default=10)
    run.add_argument(
        "--warm-up", type=int, default=10, help="votes revealed before the first batch"
    )
    run.add_argument("--max-batches", type=int, default=None, help="stop after this many per user")
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--json", dest="json_path", type=Path, help="also dump the report here")
    # Never together: rewriting the baseline from this run and then comparing this run
    # to it is a check that cannot fail.
    gate = run.add_mutually_exclusive_group()
    gate.add_argument("--check", action="store_true", help="fail when the numbers regressed")
    gate.add_argument(
        "--update-baseline", action="store_true", help="rewrite the committed numbers"
    )
    run.add_argument("--live", action="store_true", help="call the real services (says what first)")
    run.add_argument("--record", type=Path, help="write a live run's answers to a cassette")
    run.add_argument("--yes", action="store_true", help="answer the live-run question (scripts)")

    fixtures = actions.add_parser("fixtures", help="regenerate the committed vote set")
    fixtures.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    fixtures.add_argument("--seed", type=int, default=SYNTHETIC_SEED)

    imported = actions.add_parser("import", help="build a private vote set from a real database")
    imported.add_argument("--from", dest="database", type=Path, required=True)
    imported.add_argument("--source", choices=["tindarr", "suggestarr"], default=None)
    imported.add_argument("--name", default="private")
    imported.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_FIXTURES / "private" / "votes.json",
        help="where to write it; must be under a 'private/' directory",
    )


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


def evaluate_command(args: argparse.Namespace) -> int:
    """Run one ``tindarr eval`` action. Returns 1 on a regression, 2 on a mistake."""
    out = sys.stdout
    try:
        if args.action == "fixtures":
            write_fixtures(EvalPaths(args.fixtures), args.seed, out)
            return 0
        if args.action == "import":
            import_fixture(args.database, args.out, args.name, args.source, out)
            return 0
        return _eval_run(args, out)
    except EvalError as failure:
        logger.error("%s", failure)  # noqa: TRY400 - the traceback adds nothing here
        return 2


def _eval_run(args: argparse.Namespace, out: "TextIO") -> int:
    paths = EvalPaths(args.fixtures)
    options = ReplayOptions(
        batch_size=args.batch_size,
        warm_up=args.warm_up,
        max_batches=args.max_batches,
        seed=args.seed,
    )
    report = asyncio.run(
        run_evaluation(
            paths,
            args.strategy,
            options,
            live=args.live,
            confirmed=args.yes,
            record=args.record,
            out=out,
        )
    )
    if args.json_path is not None:
        write_report(report, args.json_path)
    if args.update_baseline:
        write_baseline(paths, report, out)
    return compare_to_baseline(paths, report, out) if args.check else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``tindarr`` command."""
    args = _parser().parse_args(argv)
    command = args.command or "serve"
    if command == "eval":
        configure_logging("INFO")
        return evaluate_command(args)
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
