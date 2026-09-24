"""Command line.

``tindarr [serve]`` runs the server, ``tindarr healthcheck`` probes it, and
``tindarr media-server reset`` is the host-side recovery path when the media server was
replaced and nobody can re-authenticate on it any more (docs/auth.md, section 3).

``tindarr import suggestarr`` reads the votes somebody already cast in the SuggestArr
fork and writes them into their Tindarr account, so a migrated household does not start
from an empty taste profile (roadmap step 5). It opens the fork's database **read-only**
and it is safe to run twice. It is the one import that writes *votes*: lot 4.4's file
imports write history, which is a different table and a different claim.

``tindarr eval`` is the offline evaluation harness of ADR 0013 (docs/evaluation.md). It
is a development command: it opens no instance database, it makes no network call unless
``--live`` is given and confirmed, and it exits 1 — not 2 — when the numbers regressed,
so a build can tell "worse" from "broken". ``tindarr eval session`` is the exception
that proves the rule: it is live by definition, because its whole point is to put real
cards in front of a real person, and it says what it will call and waits for a yes.
"""

import argparse
import asyncio
import logging
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from urllib.error import URLError

import uvicorn

from tindarr import __version__
from tindarr.core.config import ConfigError, ServerConfig, load_config
from tindarr.core.logs import configure_logging
from tindarr.main.app import create_app, start
from tindarr.main.evaluation import (
    DEFAULT_FIXTURES,
    PRIVATE_FIXTURES,
    STRATEGIES,
    SYNTHETIC_FIXTURES,
    EvalError,
    EvalPaths,
    compare_to_baseline,
    import_fixture,
    record_cassette,
    run_evaluation,
    write_baseline,
    write_fixtures,
    write_report,
)
from tindarr.main.session import SESSION_FIXTURES, SessionOptions, run_session
from tindarr.storage import votes as vote_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.settings import environment_overrides
from tindarr.swipe.evaluation import ReplayOptions
from tindarr.swipe.imports import suggestarr
from tindarr.swipe.strategy import NOVELTY_LEVELS, Novelty

if TYPE_CHECKING:  # pragma: no cover - imported for the annotation and nothing else
    from sqlalchemy.ext.asyncio import AsyncConnection

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
    _add_import(commands.add_parser("import", help="bring in what another tool already knows"))
    _add_eval(commands.add_parser("eval", help="offline evaluation harness (development)"))
    return parser


def _add_import(group: argparse.ArgumentParser) -> None:
    """Add ``tindarr import suggestarr``: the fork's votes, into a Tindarr account."""
    actions = group.add_subparsers(dest="action", required=True)
    fork = actions.add_parser(
        "suggestarr",
        help="import the votes cast in the SuggestArr fork's swipe deck",
        description=(
            "Read a SuggestArr database and store its swipe votes as this user's votes. "
            "The database is opened read-only and never written to; point this at a copy "
            "if the instance is running. Running it twice imports nothing twice."
        ),
    )
    fork.add_argument(
        "--from",
        dest="database",
        type=Path,
        required=True,
        help="the fork's SQLite database (read-only)",
    )
    fork.add_argument(
        "--user",
        dest="user_id",
        default=None,
        help=(
            "the Tindarr user id to import into. Needed when the fork has no linked "
            "media server account to match on, or when more than one matches"
        ),
    )
    fork.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be imported, and write nothing",
    )


def _add_eval(harness: argparse.ArgumentParser) -> None:
    """Add the actions of ``tindarr eval`` (docs/evaluation.md)."""
    actions = harness.add_subparsers(dest="action", required=True)

    run = actions.add_parser("run", help="replay a vote set past a strategy and print the table")
    run.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help=f"the vote set to walk (default: {DEFAULT_FIXTURES})",
    )
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
    run.add_argument("--live", action="store_true", help="call the real TMDb (says what first)")
    run.add_argument(
        "--live-llm",
        action="store_true",
        dest="live_llm",
        help="call the real AI provider (says where and what first)",
    )
    run.add_argument(
        "--record",
        type=Path,
        metavar="DIR",
        help="merge a live run's answers into the cassettes of this fixture directory",
    )
    run.add_argument("--yes", action="store_true", help="answer the live-run question (scripts)")

    fixtures = actions.add_parser("fixtures", help="regenerate the generated vote set")
    # Its own default, and it refuses any directory holding a vote set it did not
    # generate: the real votes next door cannot be rebuilt from a seed.
    fixtures.add_argument("--fixtures", type=Path, default=SYNTHETIC_FIXTURES)
    fixtures.add_argument("--seed", type=int, default=SYNTHETIC_SEED)

    record = actions.add_parser(
        "record", help="record a vote set's TMDb answers from the real TMDb (says what first)"
    )
    record.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    record.add_argument("--yes", action="store_true", help="answer the live-run question (scripts)")

    _add_session(
        actions.add_parser(
            "session", help="swipe real cards yourself and measure what you answered"
        )
    )

    imported = actions.add_parser("import", help="build a private vote set from a real database")
    imported.add_argument("--from", dest="database", type=Path, required=True)
    imported.add_argument("--source", choices=["tindarr", "suggestarr"], default=None)
    imported.add_argument("--name", default="private")
    imported.add_argument(
        "--out",
        type=Path,
        default=PRIVATE_FIXTURES / "votes.json",
        help="where to write it; must be under a 'private/' directory",
    )


def _add_session(session: argparse.ArgumentParser) -> None:
    """Add ``tindarr eval session``, the one measurement a person answers themselves."""
    session.add_argument(
        "--out",
        type=Path,
        default=SESSION_FIXTURES / "votes.json",
        help=f"where the votes go; must be under a 'private/' directory "
        f"(default: {SESSION_FIXTURES / 'votes.json'})",
    )
    session.add_argument("--name", default="private-session", help="the vote set's name")
    session.add_argument("--user", dest="user_id", default="you")
    session.add_argument("--batches", type=int, default=3, help="how many batches to deal")
    session.add_argument("--batch-size", type=int, default=10)
    session.add_argument("--novelty", default="balanced", choices=sorted(NOVELTY_LEVELS))
    session.add_argument("--language", default="en", help="ISO 639-1, for titles and rationales")
    session.add_argument("--region", default="US", help="ISO 3166-1, for streaming offers")
    session.add_argument(
        "--providers",
        action="store_true",
        help="ask TMDb where each card streams in the region (one more request per card)",
    )
    session.add_argument(
        "--seed-from",
        dest="seed_from",
        default=None,
        metavar="FIXTURE",
        help="an existing vote set (a name under fixtures/eval, or a path) whose votes "
        "are history before the first batch, so a session measures a profile instead of "
        "calibration",
    )
    session.add_argument("--yes", action="store_true", help="answer the live-run question")


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


def import_command(config: ServerConfig, args: argparse.Namespace) -> int:
    """Run one ``tindarr import`` action. Returns 2 on a mistake.

    The fork's database is read **before** the runtime starts. Reading it is where most
    of the ways this can go wrong live — wrong path, wrong database, no votes — and
    finding out after migrating the instance's own database would be a worse trade for
    nobody's benefit.
    """
    out = sys.stdout
    try:
        fork_votes, unreadable = suggestarr.read_votes(args.database)
        identities = suggestarr.read_identities(args.database)
    except suggestarr.ForkDatabaseError as failure:
        logger.error("%s", failure)  # noqa: TRY400 - the traceback adds nothing here
        return 2
    if not fork_votes:
        out.write(f"{args.database} holds no votes this command can read.\n")
        return 0
    try:
        return asyncio.run(
            _import_suggestarr(config, args, fork_votes, identities, unreadable, out)
        )
    except suggestarr.MappingError as failure:
        logger.error("%s", failure)  # noqa: TRY400 - the traceback adds nothing here
        return 2


async def _import_suggestarr(  # noqa: PLR0913, PLR0917 - one argument per thing already read
    config: ServerConfig,
    args: argparse.Namespace,
    fork_votes: Sequence[suggestarr.ForkVote],
    identities: Sequence[suggestarr.ForkIdentity],
    unreadable: int,
    out: TextIO,
) -> int:
    """Resolve each fork user to an account and store their votes, or say why not."""
    by_fork_user: dict[int, list[suggestarr.ForkVote]] = {}
    for vote in fork_votes:
        by_fork_user.setdefault(vote.fork_user_id, []).append(vote)
    if args.user_id is not None and len(by_fork_user) > 1:
        logger.error(
            "this database holds votes from %d different fork users, so --user would give "
            "all of them to one account. Import from a database with one user, or link the "
            "media server accounts in the fork first.",
            len(by_fork_user),
        )
        return 2
    runtime = await start(config)
    try:
        async with write_transaction(runtime.engine) as connection:
            for fork_user_id, theirs in sorted(by_fork_user.items()):
                target = await suggestarr.resolve_target(
                    connection, identities, fork_user_id, override=args.user_id
                )
                if args.dry_run:
                    await _report_dry_run(connection, target, theirs, unreadable, out)
                    continue
                report = await suggestarr.store_votes(
                    connection,
                    target,
                    theirs,
                    now=runtime.services.clock.now(),
                    unreadable=unreadable,
                )
                out.write(
                    f"{target}: imported {report.imported}, already imported "
                    f"{report.already_imported}, superseded by a newer Tindarr vote "
                    f"{report.superseded}, unreadable {report.unreadable}.\n"
                )
            if args.dry_run:
                # Nothing was written, but the receipts were read inside the write
                # transaction: roll it back so a dry run leaves the write lock clean.
                await connection.rollback()
    finally:
        await runtime.engine.dispose()
    return 0


async def _report_dry_run(
    connection: "AsyncConnection",
    target: str,
    fork_votes: Sequence[suggestarr.ForkVote],
    unreadable: int,
    out: TextIO,
) -> None:
    """Say what a real run would write, counted against the receipts already stored."""
    receipts = [suggestarr.receipt_id(vote) for vote in fork_votes]
    already = await vote_repository.seen_receipts(connection, target, receipts)
    out.write(
        f"{target}: would import {sum(1 for one in receipts if one not in already)}, "
        f"already imported {len(already)}, unreadable {unreadable}. Nothing was written.\n"
    )


def evaluate_command(args: argparse.Namespace) -> int:
    """Run one ``tindarr eval`` action. Returns 1 on a regression, 2 on a mistake."""
    out = sys.stdout
    try:
        if args.action == "fixtures":
            write_fixtures(EvalPaths(args.fixtures), args.seed, out)
            return 0
        if args.action == "record":
            asyncio.run(record_cassette(EvalPaths(args.fixtures), confirmed=args.yes, out=out))
            return 0
        if args.action == "import":
            import_fixture(args.database, args.out, args.name, args.source, out)
            return 0
        if args.action == "session":
            asyncio.run(run_session(_session_options(args), confirmed=args.yes, out=out))
            return 0
        return _eval_run(args, out)
    except EvalError as failure:
        logger.error("%s", failure)  # noqa: TRY400 - the traceback adds nothing here
        return 2


def _session_options(args: argparse.Namespace) -> SessionOptions:
    """Read one session's switches off the command line."""
    novelty: Novelty = args.novelty
    return SessionOptions(
        out=args.out,
        name=args.name,
        user_id=args.user_id,
        batches=args.batches,
        batch_size=args.batch_size,
        novelty=novelty,
        language=args.language,
        region=args.region,
        providers=args.providers,
        seed_from=args.seed_from,
    )


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
            live_llm=args.live_llm,
            confirmed=args.yes,
            record=args.record,
            out=out,
        )
    )
    if args.json_path is not None:
        write_report(report, args.json_path)
    if args.update_baseline:
        write_baseline(paths, report, out)
    return compare_to_baseline(paths, STRATEGIES[args.strategy], report, out) if args.check else 0


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
    if command == "import":
        return import_command(config, args)
    return serve(config)
