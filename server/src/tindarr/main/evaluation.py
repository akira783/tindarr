"""``tindarr eval``: the composition root of the offline harness.

Everything HTTP-shaped about the harness lives here, because the domain may not know
what HTTP is. This module picks a strategy by name, builds the ``Metadata`` port either
from a recorded cassette or — only behind ``--live``, only after saying what it will
call — from the real TMDb, and prints the table.

**Nothing here spends money by accident.** The default path has no network at all: the
transport is a cassette, and a request it does not hold raises rather than dialling out.
``--live`` prints the plan first and waits for a ``--yes`` or an answer at the terminal.

The fixture builder is here for the same reason: regenerating the committed cassette
means recording the real adapter's own requests, and only this layer may touch an
adapter.
"""

import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO

import httpx2

from tindarr.adapters.cassette import Cassette, CassetteMissError, RecordingTransport
from tindarr.adapters.tmdb import TmdbMetadata
from tindarr.ports.metadata import Metadata, Title
from tindarr.ports.titles import TitleRef, as_media_kind
from tindarr.swipe.baselines import PopularBaseline, RandomBaseline
from tindarr.swipe.evaluation import (
    Baseline,
    BaselineError,
    CostMeter,
    CountingMetadata,
    DatasetError,
    EvalDataset,
    EvaluationReport,
    ReplayOptions,
    check,
    evaluate,
    load_dataset,
)
from tindarr.swipe.evaluation.dataset import CatalogEntry
from tindarr.swipe.evaluation.importing import ImportSummary, VoteImportError, import_votes
from tindarr.swipe.evaluation.synthetic import build_synthetic_dataset
from tindarr.swipe.strategy import Strategy

__all__ = [
    "STRATEGIES",
    "EvalPaths",
    "build_cassette",
    "import_fixture",
    "private_path",
    "run_evaluation",
    "write_fixtures",
]

logger = logging.getLogger(__name__)

#: Where the committed fixture lives, relative to ``server/``.
DEFAULT_FIXTURES: Final = Path("fixtures/eval")
#: The environment variable a live run reads its TMDb key from. Deliberately not the
#: connector's stored key: a development command does not open the instance's database.
LIVE_KEY_VARIABLE: Final = "TINDARR_EVAL_TMDB_API_KEY"
#: The only directory name an imported, private vote set may be written into.
PRIVATE_DIRECTORY: Final = "private"
#: ``/3/<kind>/<id>``, the only shape the fixture service answers.
_DETAILS_PARTS: Final = 2


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """One strategy the harness can run, and what a live run of it would cost."""

    name: str
    build: Callable[[Sequence[Title], Metadata], Strategy]
    #: What to tell the operator before a live run. Both baselines read TMDb once per
    #: proposed card and never call a model; a strategy that does will say so here.
    metadata_calls_per_card: int = 1
    llm_calls_per_batch: int = 0


STRATEGIES: Final[Mapping[str, StrategySpec]] = {
    "popular": StrategySpec("popular", PopularBaseline),
    "random": StrategySpec("random", RandomBaseline),
}


@dataclass(frozen=True, slots=True)
class EvalPaths:
    """The files one fixture directory holds."""

    root: Path

    @property
    def votes(self) -> Path:
        """The vote set."""
        return self.root / "votes.json"

    @property
    def cassette(self) -> Path:
        """The recorded TMDb answers an offline run replays."""
        return self.root / "tmdb.json"

    def baseline(self, strategy: str) -> Path:
        """Return the committed numbers for one strategy."""
        return self.root / f"baseline-{strategy}.json"


class EvalError(RuntimeError):
    """Something the operator can fix, reported as one line and exit code 2."""


# --- running -------------------------------------------------------------------------


async def run_evaluation(  # noqa: PLR0913 - one command's worth of switches
    paths: EvalPaths,
    strategy: str,
    options: ReplayOptions,
    *,
    live: bool = False,
    confirmed: bool = False,
    record: Path | None = None,
    out: TextIO = sys.stdout,
) -> EvaluationReport:
    """Replay the fixture past ``strategy`` and return the report, printing the table."""
    spec = _spec(strategy)
    dataset = _dataset(paths.votes)
    recorder = None
    if live:
        _confirm_live(spec, dataset, options, confirmed=confirmed, out=out)
        recorder = RecordingTransport(httpx2.AsyncHTTPTransport(), provider="tmdb")
        transport: httpx2.AsyncBaseTransport = recorder
        api_key = _live_key()
    else:
        transport = _cassette(paths.cassette).transport()
        api_key = "offline"

    meter = CostMeter()
    metadata = CountingMetadata(TmdbMetadata(api_key, transport=transport), meter)
    pool = dataset.pool
    try:
        report = await evaluate(
            dataset, lambda: spec.build(pool, metadata), spec.name, meter, options
        )
    except CassetteMissError as missing:
        raise EvalError(
            f"the cassette has no answer for '{missing.key}'. Re-record it with "
            "'tindarr eval fixtures', or run with --live."
        ) from None
    finally:
        if recorder is not None:
            if record is not None:
                recorder.cassette.write(record)
                logger.info("recorded %d answers", len(recorder.cassette))
            await recorder.aclose()
    out.write(report.table())
    return report


def _spec(strategy: str) -> StrategySpec:
    spec = STRATEGIES.get(strategy)
    if spec is None:
        raise EvalError(f"unknown strategy '{strategy}'; expected {' or '.join(STRATEGIES)}")
    return spec


def _dataset(path: Path) -> EvalDataset:
    try:
        return load_dataset(path)
    except DatasetError as failure:
        raise EvalError(f"{path}: {failure}") from None


def _cassette(path: Path) -> Cassette:
    try:
        return Cassette.load(path)
    except OSError:
        raise EvalError(
            f"no recorded answers at {path}. Build the fixture with 'tindarr eval fixtures', "
            "or run with --live."
        ) from None
    except ValueError as failure:
        raise EvalError(f"{path}: {failure}") from None


def _live_key() -> str:
    import os  # noqa: PLC0415 - read at the moment of use, never cached in a module

    key = os.environ.get(LIVE_KEY_VARIABLE, "").strip()
    if not key:
        raise EvalError(f"a live run needs a TMDb key in {LIVE_KEY_VARIABLE}")
    return key


def expected_batches(dataset: EvalDataset, options: ReplayOptions) -> int:
    """How many batches a run will ask for. Printed before a live run charges for them."""
    total = 0
    for user in dataset.users:
        remaining = len(user.votes) - options.warm_up
        if remaining <= 0:
            continue
        batches = -(-remaining // options.batch_size)
        total += min(batches, options.max_batches) if options.max_batches else batches
    return total


def live_plan(spec: StrategySpec, dataset: EvalDataset, options: ReplayOptions) -> str:
    """Say, before anything is called, which service will be reached and what it costs."""
    batches = expected_batches(dataset, options)
    cards = batches * options.batch_size
    lines = [
        "A live run reaches real services. This one would call:",
        f"  TMDb        up to {cards * spec.metadata_calls_per_card} requests "
        f"({batches} batches of {options.batch_size}). TMDb's API is free for personal "
        "use; its rate limits and caching terms apply.",
    ]
    if spec.llm_calls_per_batch:
        lines.append(
            f"  AI provider up to {batches * spec.llm_calls_per_batch} generations, "
            "billed by whichever provider the key belongs to. The harness cannot know "
            "the price; read it on your provider's dashboard before answering."
        )
    else:
        lines.append("  AI provider none: this strategy calls no model, so nothing is billed.")
    return "\n".join(lines) + "\n"


def _confirm_live(
    spec: StrategySpec,
    dataset: EvalDataset,
    options: ReplayOptions,
    *,
    confirmed: bool,
    out: TextIO,
) -> None:
    out.write(live_plan(spec, dataset, options))
    if confirmed:
        return
    if not sys.stdin.isatty():  # pragma: no cover - only reached from a terminal
        raise EvalError("a live run needs --yes when it cannot ask")
    answer = input("Type 'yes' to make these calls: ")  # pragma: no cover - interactive
    if answer.strip() != "yes":  # pragma: no cover - interactive
        raise EvalError("nothing was called")


# --- the gate ------------------------------------------------------------------------


def compare_to_baseline(paths: EvalPaths, report: EvaluationReport, out: TextIO) -> int:
    """Hold the report to the committed numbers; return the process's exit code."""
    path = paths.baseline(report.strategy)
    try:
        baseline = Baseline.load(path)
        regressions = check(baseline, report)
    except BaselineError as failure:
        raise EvalError(f"{path}: {failure}") from None
    if not regressions:
        out.write(f"\nno regression against {path}\n")
        return 0
    out.write(f"\n{len(regressions)} number(s) worse than {path}:\n")
    out.writelines(f"  {regression}\n" for regression in regressions)
    out.write(
        "\nIf this is an improvement the numbers do not show, or a deliberate trade, "
        "update the baseline with 'tindarr eval run --strategy "
        f"{report.strategy} --update-baseline' and say why in the commit message.\n"
    )
    return 1


def write_baseline(paths: EvalPaths, report: EvaluationReport, out: TextIO) -> None:
    """Replace the committed numbers for this strategy with the ones just measured."""
    path = paths.baseline(report.strategy)
    Baseline.of(report).write(path)
    out.write(f"\nbaseline written to {path}\n")


# --- fixtures ------------------------------------------------------------------------


def write_fixtures(paths: EvalPaths, seed: int, out: TextIO) -> EvalDataset:
    """Regenerate the committed vote set and the cassette that goes with it."""
    dataset = build_synthetic_dataset(seed)
    dataset.write(paths.votes)
    cassette = build_cassette(dataset)
    cassette.write(paths.cassette)
    out.write(
        f"{paths.votes}: {sum(len(user.votes) for user in dataset.users)} votes, "
        f"{len(dataset.catalog)} titles\n{paths.cassette}: {len(cassette)} recorded answers\n"
    )
    return dataset


def build_cassette(dataset: EvalDataset) -> Cassette:
    """Record the TMDb answers an offline replay of ``dataset`` needs.

    The recording is made through the **real** adapter, against a service that answers
    from the catalogue, so the keys in the file are exactly the ones the adapter will
    ask for later. Deriving them by hand is how a cassette silently stops matching.
    """
    import asyncio  # noqa: PLC0415 - only the fixture builder blocks on a loop

    async def record() -> Cassette:
        transport = RecordingTransport(_catalog_service(dataset), provider="tmdb")
        tmdb = TmdbMetadata("fixture", transport=transport)
        for entry in sorted(dataset.catalog, key=lambda row: row.ref):
            await tmdb.details(entry.ref, dataset.language)
        await transport.aclose()
        return transport.cassette

    return asyncio.run(record())


def _catalog_service(dataset: EvalDataset) -> httpx2.MockTransport:
    """TMDb, answering from the fixture's own catalogue. Only the fixture builder uses it."""
    catalog = dataset.by_ref

    def handle(request: httpx2.Request) -> httpx2.Response:
        parts = request.url.path.removeprefix("/3/").split("/")
        kind = as_media_kind(parts[0]) if parts else None
        entry = (
            catalog.get(TitleRef(kind, int(parts[1])))
            if kind is not None and len(parts) == _DETAILS_PARTS and parts[1].isdigit()
            else None
        )
        if entry is None:
            return httpx2.Response(404, json={"status_message": "The resource you requested."})
        return httpx2.Response(200, json=_tmdb_payload(entry))

    return httpx2.MockTransport(handle)


def _tmdb_payload(entry: CatalogEntry) -> dict[str, Any]:
    """One catalogue entry, in TMDb's own shape."""
    date = f"{entry.year}-06-01" if entry.year else None
    payload: dict[str, Any] = {
        "id": entry.tmdb_id,
        "overview": entry.overview,
        "genres": [{"id": 1000 + index, "name": name} for index, name in enumerate(entry.genres)],
        "poster_path": f"/{entry.tmdb_id}.jpg",
        "original_language": entry.original_language,
        "vote_average": entry.vote_average,
        "vote_count": entry.vote_count,
        "adult": entry.adult,
    }
    if entry.kind == "movie":
        payload |= {
            "title": entry.title,
            "original_title": entry.title,
            "release_date": date,
            "runtime": 100,
            "imdb_id": f"tt{entry.tmdb_id}",
        }
    else:
        payload |= {
            "name": entry.title,
            "original_name": entry.title,
            "first_air_date": date,
            "number_of_seasons": 2,
            "number_of_episodes": 16,
            "external_ids": {"imdb_id": f"tt{entry.tmdb_id}"},
        }
    return payload


# --- importing a private vote set ----------------------------------------------------


def private_path(path: Path) -> Path:
    """Return ``path``, refusing one that is not inside a directory called ``private``.

    A vote set read out of somebody's instance is their viewing history. The rule is a
    blunt one on purpose: a file the repository ignores by name cannot be committed by
    an ``git add .`` on a tired evening.
    """
    if PRIVATE_DIRECTORY not in path.parts:
        raise EvalError(
            f"{path} is not under a '{PRIVATE_DIRECTORY}/' directory. An imported vote set is "
            "somebody's viewing history; it goes somewhere the repository ignores."
        )
    return path


def import_fixture(
    database: Path, out_path: Path, name: str, source: str | None, out: TextIO
) -> ImportSummary:
    """Build a private vote set from a real instance's database."""
    target = private_path(out_path)
    try:
        dataset, summary = import_votes(database, name, source)
    except VoteImportError as failure:
        raise EvalError(str(failure)) from None
    dataset.write(target)
    out.write(f"{summary}\n{target}\n")
    out.write(
        "This file holds real viewing history. It is not for the repository, and the "
        "harness reads it with --fixtures pointing at its directory.\n"
    )
    return summary


def write_report(report: EvaluationReport, path: Path) -> None:
    """Dump the report as JSON, for a dashboard or a diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json(), encoding="utf-8")
