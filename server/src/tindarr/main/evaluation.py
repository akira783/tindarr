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

from tindarr.adapters.cassette import (
    Cassette,
    CassetteMissError,
    Interaction,
    RecordingTransport,
    TopUpTransport,
)
from tindarr.adapters.llm.factory import llm_provider_factory
from tindarr.adapters.tmdb import TmdbMetadata
from tindarr.ports.llm import LlmConnection, LlmProvider
from tindarr.ports.metadata import DiscoverQuery, Metadata
from tindarr.ports.titles import TitleRef, as_media_kind
from tindarr.swipe.baselines import PopularBaseline, RandomBaseline
from tindarr.swipe.evaluation import (
    Baseline,
    BaselineError,
    CostMeter,
    CountingLlmProvider,
    CountingMetadata,
    DatasetError,
    EvalDataset,
    EvaluationReport,
    Regression,
    ReplayOptions,
    check,
    check_floor,
    evaluate,
    load_dataset,
    uncomparable,
)
from tindarr.swipe.evaluation.dataset import CatalogEntry
from tindarr.swipe.evaluation.gate import FLOOR_STRATEGIES
from tindarr.swipe.evaluation.importing import ImportSummary, VoteImportError, import_votes
from tindarr.swipe.evaluation.synthetic import SYNTHETIC_NAME, build_synthetic_dataset
from tindarr.swipe.hybrid import HybridStrategy
from tindarr.swipe.retrieval import NOVELTY_BANDS, POOL_SIZE, Retrieval
from tindarr.swipe.strategy import Strategy

__all__ = [
    "AUTHOR_FIXTURES",
    "PRIVATE_FIXTURES",
    "STRATEGIES",
    "SYNTHETIC_FIXTURES",
    "EvalPaths",
    "Parts",
    "StrategySpec",
    "build_cassette",
    "catalog_service",
    "hybrid_strategy",
    "import_fixture",
    "live_plan",
    "merge",
    "popular_baseline",
    "private_path",
    "random_baseline",
    "record_cassette",
    "rehost",
    "run_evaluation",
    "write_fixtures",
]

logger = logging.getLogger(__name__)

#: Where the committed vote sets live, relative to ``server/``. One directory per vote
#: set, named after the set it holds — which is the name printed at the top of every
#: report, so a number on a terminal and a directory in a diff cannot be confused.
FIXTURES_ROOT: Final = Path("fixtures/eval")
#: The author's own 99 votes, published with their consent. The set ADR 0013 was
#: measured on, and what ``tindarr eval run`` walks when nobody says otherwise.
AUTHOR_FIXTURES: Final = FIXTURES_ROOT / "akira-99"
#: The generated vote set. Invented from a seed, so it proves the harness computes what
#: it claims to and nothing about what a real person would enjoy.
SYNTHETIC_FIXTURES: Final = FIXTURES_ROOT / "synthetic-99"
DEFAULT_FIXTURES: Final = AUTHOR_FIXTURES
#: The environment variable a live run reads its TMDb key from. Deliberately not the
#: connector's stored key: a development command does not open the instance's database.
LIVE_KEY_VARIABLE: Final = "TINDARR_EVAL_TMDB_API_KEY"
#: Where a live model run sends its prompts. There is no default: an AI provider costs
#: money at most addresses, so the operator types the one they mean.
LLM_BASE_VARIABLE: Final = "TINDARR_EVAL_LLM_BASE_URL"
LLM_MODEL_VARIABLE: Final = "TINDARR_EVAL_LLM_MODEL"
LLM_KEY_VARIABLE: Final = "TINDARR_EVAL_LLM_API_KEY"
#: The model the committed answers were recorded from. A run against another model asks
#: another question and needs its own recording: the model id is part of the request,
#: and therefore part of the key a cassette is looked up by.
RECORDED_LLM_MODEL: Final = "gpt-5.6-luna"
#: The address a recorded model answer is filed under, whatever address produced it.
#:
#: An OpenAI-compatible endpoint is somebody's own machine, so its host is a fact about
#: their network and has no business in a public repository — and a cassette keyed on it
#: would only replay for them. Recorded keys are rewritten to this address, and an
#: offline run dials it, which resolves nowhere on purpose.
RECORDED_LLM_BASE: Final = "http://recorded.invalid/v1"
#: The only directory name an imported, private vote set may be written into.
PRIVATE_DIRECTORY: Final = "private"
#: Where ``eval import`` writes by default. ``.gitignore`` keeps it out of the repository.
PRIVATE_FIXTURES: Final = FIXTURES_ROOT / PRIVATE_DIRECTORY
#: ``/3/<kind>/<id>``, and ``/3/discover/<kind>``: two path segments.
_DETAILS_PARTS: Final = 2
#: ``/3/<kind>/<id>/recommendations``.
_RELATED_PARTS: Final = 3
#: TMDb answers twenty results a page, and so does the fixture service.
_PAGE_SIZE: Final = 20


@dataclass(frozen=True, slots=True)
class Parts:
    """What a strategy is built from, once per user.

    One object rather than three arguments because the strategies do not agree on which
    of them they need, and a factory signature that changes every time one is added is
    a factory signature nobody can implement.
    """

    retrieval: Retrieval
    metadata: Metadata
    #: ``None`` for a strategy that calls no model. A strategy that needs one says so in
    #: its spec, and the harness refuses to build it without one rather than handing it
    #: a stand-in that would quietly score as "the model answered nothing".
    llm: LlmProvider | None = None


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """One strategy the harness can run, and what a live run of it would cost."""

    name: str
    build: Callable[[Parts], Strategy]
    #: Whether a live run of it reaches an AI provider.
    needs_llm: bool = False
    #: What to tell the operator before a live run. Every strategy reads TMDb once per
    #: proposed card for its details, and the retrieval layer costs a handful of list
    #: calls per batch on top — fewer after the first, because the pages are cached for
    #: the user's run.
    metadata_calls_per_card: int = 1
    metadata_calls_per_batch: int = 8
    llm_calls_per_batch: int = 0


def popular_baseline(parts: Parts) -> Strategy:
    """Build the "most popular candidate in the pool" floor."""
    return PopularBaseline(parts.retrieval, parts.metadata)


def random_baseline(parts: Parts) -> Strategy:
    """Build the "whatever the pool happens to hold" floor."""
    return RandomBaseline(parts.retrieval, parts.metadata)


def hybrid_strategy(parts: Parts) -> Strategy:
    """Build the engine of ADR 0013: retrieval, then the model."""
    if parts.llm is None:  # pragma: no cover - the caller checks before it builds
        raise EvalError("the hybrid strategy needs an AI provider")
    return HybridStrategy(parts.retrieval, parts.metadata, parts.llm)


STRATEGIES: Final[Mapping[str, StrategySpec]] = {
    "popular": StrategySpec("popular", popular_baseline),
    "random": StrategySpec("random", random_baseline),
    # One model call a batch, and one more only when the first answer failed its schema
    # (``tindarr.adapters.llm.base``). The live plan quotes the first number.
    "hybrid": StrategySpec("hybrid", hybrid_strategy, needs_llm=True, llm_calls_per_batch=1),
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

    @property
    def llm_cassette(self) -> Path:
        """The recorded model answers an offline run of a model strategy replays."""
        return self.root / "llm.json"

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
    live_llm: bool = False,
    confirmed: bool = False,
    record: Path | None = None,
    out: TextIO = sys.stdout,
) -> EvaluationReport:
    """Replay the fixture past ``strategy`` and return the report, printing the table.

    Two independent taps. ``live`` reaches the real TMDb; ``live_llm`` reaches the real
    AI provider. Either one on means the plan is printed and confirmed first, and a run
    with neither makes no network call at all — the transports are cassettes, and a
    request they do not hold raises instead of dialling out.
    """
    spec = _spec(strategy)
    dataset = _dataset(paths.votes)
    wants_model = live_llm and spec.needs_llm
    if live_llm and not spec.needs_llm:
        raise EvalError(
            f"the '{spec.name}' strategy calls no model, so --live-llm asks for nothing"
        )
    if live or wants_model:
        out.write(live_plan(spec, dataset, options, live=live, live_llm=wants_model))
        _answered(confirmed=confirmed)
    recorders: dict[str, RecordingTransport] = {}
    # The keys first: a transport opened before its credential is checked is a
    # connection pool nobody closes when the run gives up, and an unraisable warning
    # later, in whichever test the garbage collector happens to land on.
    api_key = _live_key() if live else "offline"
    connection = _llm_connection(live=wants_model) if spec.needs_llm else None
    try:
        transport = _tmdb_transport(paths, recorders, live=live)
        llm = _llm_provider(paths, connection, recorders, live=wants_model)
        report = await _replay(dataset, spec, _Ports(transport, api_key, llm), options)
    except CassetteMissError as missing:
        raise EvalError(
            f"the cassette has no answer for '{missing.key}'. Re-record it with "
            "'tindarr eval fixtures', or run with --live / --live-llm and --record."
        ) from None
    finally:
        await _finish(
            recorders, paths, record, connection.base_url or "" if connection else "", out
        )
    out.write(report.table())
    return report


@dataclass(frozen=True, slots=True)
class _Ports:
    """What the replay talks through: one transport per provider, plus the TMDb key."""

    transport: httpx2.AsyncBaseTransport
    tmdb_key: str
    llm: LlmProvider | None


async def _replay(
    dataset: EvalDataset, spec: StrategySpec, ports: _Ports, options: ReplayOptions
) -> EvaluationReport:
    """Build the metered ports and walk the vote set past a fresh strategy per user."""
    meter = CostMeter()
    metadata = CountingMetadata(TmdbMetadata(ports.tmdb_key, transport=ports.transport), meter)
    counted = CountingLlmProvider(ports.llm, meter) if ports.llm is not None else None

    def build() -> Strategy:
        # A retrieval per user, like a strategy: its cache holds nothing private, but an
        # object two people share is an object that can carry one person's state into
        # the other's batch, and the replay's guarantees are not worth that.
        return spec.build(Parts(Retrieval(metadata, POOL_SIZE), metadata, counted))

    return await evaluate(dataset, build, spec.name, meter, options)


def _tmdb_transport(
    paths: EvalPaths, recorders: dict[str, RecordingTransport], *, live: bool
) -> httpx2.AsyncBaseTransport:
    if not live:
        return _cassette(paths.cassette).transport()
    recorder = RecordingTransport(_topped_up(paths.cassette), provider="tmdb")
    recorders["tmdb"] = recorder
    return recorder


def _topped_up(path: Path) -> httpx2.AsyncBaseTransport:
    """Return the real service, with the committed answers in front of it.

    A live run extends a fixture; it does not replace one. Reading what is already
    recorded first is what makes a second strategy's recording safe for the first
    strategy's numbers (``TopUpTransport``).
    """
    return TopUpTransport(_existing(path), httpx2.AsyncHTTPTransport())


def _llm_connection(*, live: bool) -> LlmConnection:
    """Where a model run talks, and as what.

    Only ``openai_compatible``: the harness is a development command run against a local
    endpoint, and a kind that reaches a named vendor by default is a kind that bills
    somebody by accident. The address is typed by the operator, printed in the plan
    before anything is sent, and never written into a recording.
    """
    import os  # noqa: PLC0415 - read at the moment of use, never cached in a module

    if not live:
        return LlmConnection(
            kind="openai_compatible",
            api_key="offline",
            base_url=RECORDED_LLM_BASE,
            model=os.environ.get(LLM_MODEL_VARIABLE, "").strip() or RECORDED_LLM_MODEL,
        )
    base_url = os.environ.get(LLM_BASE_VARIABLE, "").strip()
    if not base_url:
        raise EvalError(
            f"a live model run needs an OpenAI-compatible address in {LLM_BASE_VARIABLE}"
        )
    return LlmConnection(
        kind="openai_compatible",
        api_key=os.environ.get(LLM_KEY_VARIABLE, "").strip() or "not-needed",
        base_url=base_url,
        model=os.environ.get(LLM_MODEL_VARIABLE, "").strip() or RECORDED_LLM_MODEL,
    )


def _llm_provider(
    paths: EvalPaths,
    connection: LlmConnection | None,
    recorders: dict[str, RecordingTransport],
    *,
    live: bool,
) -> LlmProvider | None:
    if connection is None:
        return None
    if live:
        # The recorded model answers are keyed by a digest of the prompt, so a prompt
        # that has not changed costs nothing to re-record and comes back identical.
        stored = rehost(_existing(paths.llm_cassette), RECORDED_LLM_BASE, connection.base_url or "")
        recorder = RecordingTransport(
            TopUpTransport(stored, httpx2.AsyncHTTPTransport()), provider="llm"
        )
        recorders["llm"] = recorder
        transport: httpx2.AsyncBaseTransport = recorder
    else:
        transport = _cassette(paths.llm_cassette).transport()
    return llm_provider_factory(transport)(connection)


async def _finish(
    recorders: Mapping[str, RecordingTransport],
    paths: EvalPaths,
    record: Path | None,
    live_base: str,
    out: TextIO,
) -> None:
    """Write what was recorded, then close every transport that was opened."""
    for name, recorder in recorders.items():
        if record is not None:
            target = _record_target(record, paths, name)
            cassette = recorder.cassette
            if name == "llm" and live_base:
                cassette = rehost(cassette, live_base, RECORDED_LLM_BASE)
            merged = merge(_existing(target), cassette)
            merged.write(target)
            out.write(f"{target}: {len(merged)} recorded answers\n")
        await recorder.aclose()


def _record_target(record: Path, paths: EvalPaths, provider: str) -> Path:
    """Where one provider's recording goes.

    ``--record`` names a directory, because a run of a model strategy produces two
    cassettes and a single path could only hold one of them.
    """
    name = paths.cassette.name if provider == "tmdb" else paths.llm_cassette.name
    return record / name


def _existing(path: Path) -> Cassette:
    try:
        return Cassette.load(path)
    except (OSError, ValueError):
        return Cassette()


def merge(existing: Cassette, recorded: Cassette) -> Cassette:
    """Return both cassettes as one, the new answer winning where they disagree.

    Merging rather than replacing, because one fixture directory is recorded by several
    runs — one per strategy, each asking TMDb for its own pages — and a second run that
    threw away the first one's answers would leave the other strategies unable to
    replay. Delete the file to start a recording from nothing.
    """
    keys = {interaction.key for interaction in recorded.interactions}
    kept = [row for row in existing.interactions if row.key not in keys]
    provider = recorded.provider or existing.provider
    return Cassette([*kept, *recorded.interactions], provider=provider)


def rehost(cassette: Cassette, was: str, now: str) -> Cassette:
    """Return the cassette with ``was`` replaced by ``now`` at the head of every key.

    A recording made against somebody's own endpoint would otherwise carry their host
    name into the repository and replay for nobody else. Only the base address moves:
    the rest of the path, the query and the body digest are what the key is *for*.
    """
    # Spelled the way ``request_key`` spells an address — scheme, host, path, and no
    # port — because that is what a recorded key holds, not what was typed.
    before, after = _key_address(was), _key_address(now)
    moved: list[Interaction] = []
    for interaction in cassette.interactions:
        key = _rehosted(interaction.key, before, after)
        if key == interaction.key:
            # A key nobody can rewrite is a key that would carry somebody's own host
            # into a public repository, which is the one thing this function exists to
            # prevent. It refuses rather than writing the file and hoping a reviewer
            # notices the address.
            raise EvalError(
                f"a recorded answer is not filed under {before} and cannot be made "
                "portable; nothing was written"
            )
        moved.append(
            Interaction(
                key=key,
                status=interaction.status,
                body=interaction.body,
                content_type=interaction.content_type,
            )
        )
    return Cassette(moved, provider=cassette.provider)


def _key_address(base_url: str) -> str:
    """Return ``base_url`` as ``request_key`` would have written it."""
    url = httpx2.URL(base_url.rstrip("/"))
    return f"{url.scheme}://{url.host}{url.path}".rstrip("/")


def _rehosted(key: str, before: str, after: str) -> str:
    method, _, rest = key.partition(" ")
    if not rest.startswith(before):
        # Not the address this recording was supposed to reach. Left alone rather than
        # rewritten: a key nobody can explain must not be made to look like one we can.
        return key
    return f"{method} {after}{rest[len(before) :]}"


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


def live_plan(
    spec: StrategySpec,
    dataset: EvalDataset,
    options: ReplayOptions,
    *,
    live: bool,
    live_llm: bool,
) -> str:
    """Say, before anything is called, which service will be reached and what it costs."""
    import os  # noqa: PLC0415 - read at the moment of use, never cached in a module

    batches = expected_batches(dataset, options)
    cards = batches * options.batch_size
    lines = ["A live run reaches real services. This one would call:"]
    if live:
        requests = cards * spec.metadata_calls_per_card + batches * spec.metadata_calls_per_batch
        lines.append(
            f"  TMDb        up to {requests} requests ({batches} batches of "
            f"{options.batch_size}: the details of each card, plus the discovery and "
            "recommendation pages the pool is built from, which are cached per user). "
            "TMDb's API is free for personal use; its rate limits and caching terms apply."
        )
    else:
        lines.append("  TMDb        none: the recorded answers are replayed.")
    if live_llm:
        lines.append(
            f"  AI provider up to {batches * spec.llm_calls_per_batch} generations, sent to "
            f"{os.environ.get(LLM_BASE_VARIABLE, '').strip() or '(nothing configured)'}, "
            "and one more for any answer that fails its schema. Whoever owns that address "
            "decides what it costs; the harness cannot know, and a hosted endpoint bills "
            "by the token. Read the price before answering."
        )
    elif spec.needs_llm:
        lines.append("  AI provider none: the recorded answers are replayed.")
    else:
        lines.append("  AI provider none: this strategy calls no model, so nothing is billed.")
    return "\n".join(lines) + "\n"


def _answered(*, confirmed: bool) -> None:
    """Wait for a ``yes`` at the terminal, unless ``--yes`` already said it."""
    if confirmed:
        return
    if not sys.stdin.isatty():  # pragma: no cover - only reached from a terminal
        raise EvalError("a live run needs --yes when it cannot ask")
    answer = input("Type 'yes' to make these calls: ")  # pragma: no cover - interactive
    if answer.strip() != "yes":  # pragma: no cover - interactive
        raise EvalError("nothing was called")


# --- the gate ------------------------------------------------------------------------


def compare_to_baseline(
    paths: EvalPaths, spec: StrategySpec, report: EvaluationReport, out: TextIO
) -> int:
    """Hold the report to its own baseline, to the reference floors, and to its spec.

    Three questions, because two are not enough. "Did this strategy get worse than it
    was?" is answered by its own baseline. "Is it better than doing something stupid?"
    is answered by the floors — without it the first baseline a new strategy writes is
    whatever it scored, which certifies anything. And "does it spend what it said it
    would?" is answered by the ``StrategySpec``, which is the one cost bound a first
    baseline cannot write for itself: the floors call no model, so the floor comparison
    has to exempt cost, and a strategy's own first baseline is whatever it happened to
    spend.
    """
    path = paths.baseline(report.strategy)
    try:
        floors = _floors(paths)
        baseline = Baseline.load(path)
        regressions: list[Regression] = [
            *check(baseline, report),
            *check_floor(floors, report),
            *_over_budget(spec, report),
        ]
        skipped = uncomparable(floors, report)
    except BaselineError as failure:
        raise EvalError(f"{path}: {failure}") from None
    if skipped:
        # Said out loud, every time. A comparison nobody knows was skipped is a gate
        # that has quietly switched itself off.
        out.write("\nfloor comparisons not made:\n")
        out.writelines(f"  {line}\n" for line in skipped)
    if not regressions:
        out.write(f"\nno regression against {path}, and no worse than the floors\n")
        return 0
    out.write(f"\n{len(regressions)} number(s) worse than {path} or than the floors:\n")
    out.writelines(f"  {regression}\n" for regression in regressions)
    out.write(
        "\nIf this is an improvement the numbers do not show, or a deliberate trade, "
        "update the baseline with 'tindarr eval run --strategy "
        f"{report.strategy} --update-baseline' and say why in the commit message.\n"
    )
    return 1


#: How far a run may exceed the model cost its spec declares. A quarter of a call a
#: batch: a retry that rescued one answer in nine is not a change of plan, a second call
#: on every batch is.
SPEC_CALL_TOLERANCE: Final = 0.25


def _over_budget(spec: StrategySpec, report: EvaluationReport) -> list[Regression]:
    """Hold a run to the model cost its own registration declares.

    The number in ``STRATEGIES`` is what the live plan quotes to an operator before it
    spends their money, so it is a promise already made. Measuring against it costs
    nothing and closes the one gap the floor exemption opens.
    """
    spent = report.metrics.get("llm_calls_per_batch")
    allowed = spec.llm_calls_per_batch + SPEC_CALL_TOLERANCE
    if spent is None or spent <= allowed:
        return []
    return [Regression("spec.llm_calls_per_batch", float(spec.llm_calls_per_batch), spent, allowed)]


def _floors(paths: EvalPaths) -> list[Baseline]:
    """Return the committed numbers of the reference strategies, as far as they exist."""
    floors: list[Baseline] = []
    for name in FLOOR_STRATEGIES:
        path = paths.baseline(name)
        if path.is_file():
            floors.append(Baseline.load(path))
    return floors


def write_baseline(paths: EvalPaths, report: EvaluationReport, out: TextIO) -> None:
    """Replace the committed numbers for this strategy with the ones just measured."""
    path = paths.baseline(report.strategy)
    Baseline.of(report).write(path)
    out.write(f"\nbaseline written to {path}\n")
    out.write(
        "Check it against the floors before committing it: "
        f"'tindarr eval run --strategy {report.strategy} --check'.\n"
    )


# --- fixtures ------------------------------------------------------------------------


def write_fixtures(paths: EvalPaths, seed: int, out: TextIO) -> EvalDataset:
    """Regenerate the generated vote set and the cassette that goes with it."""
    _only_the_generated_set(paths)
    dataset = build_synthetic_dataset(seed)
    dataset.write(paths.votes)
    cassette = build_cassette(dataset)
    cassette.write(paths.cassette)
    out.write(
        f"{paths.votes}: {sum(len(user.votes) for user in dataset.users)} votes, "
        f"{len(dataset.catalog)} titles\n{paths.cassette}: {len(cassette)} recorded answers\n"
    )
    return dataset


def _only_the_generated_set(paths: EvalPaths) -> None:
    """Refuse to rebuild on top of a vote set that no seed can produce again.

    The vote sets are sibling directories holding the same file names, and one of them
    is somebody's real votes. A mistyped ``--fixtures`` would replace them with invented
    titles, and the diff would look like any other regeneration.
    """
    if not paths.votes.is_file():
        return
    try:
        existing = load_dataset(paths.votes)
    except DatasetError:
        return
    if existing.name != SYNTHETIC_NAME:
        raise EvalError(
            f"{paths.votes} holds the '{existing.name}' vote set, which is not generated. "
            f"'eval fixtures' rebuilds '{SYNTHETIC_NAME}' and nothing else: point "
            f"--fixtures at {SYNTHETIC_FIXTURES}."
        )


def build_cassette(dataset: EvalDataset) -> Cassette:
    """Record the TMDb answers an offline replay of ``dataset`` needs.

    The recording is made through the **real** adapter, against a service that answers
    from the catalogue, so the keys in the file are exactly the ones the adapter will
    ask for later. Deriving them by hand is how a cassette silently stops matching.

    Three sets of answers, because a strategy asks for three things: the details of
    every title (any of them can end up on a card), every discovery page any novelty
    band reads, and the recommendations of every title (any of them can be something
    this user liked). Recording what one strategy happened to ask for would mean a live
    run for the next one.
    """
    import asyncio  # noqa: PLC0415 - only the fixture builder blocks on a loop

    async def record() -> Cassette:
        transport = RecordingTransport(catalog_service(dataset), provider="tmdb")
        tmdb = TmdbMetadata("fixture", transport=transport)
        await tmdb.genres()
        for entry in sorted(dataset.catalog, key=lambda row: row.ref):
            await tmdb.details(entry.ref, dataset.language)
            await tmdb.related(entry.ref, dataset.language)
        for query in _fixture_queries(dataset):
            await tmdb.discover(query)
        await transport.aclose()
        return transport.cassette

    return asyncio.run(record())


def _fixture_queries(dataset: EvalDataset) -> list[DiscoverQuery]:
    """Every discovery page the retrieval layer can ask this vote set for.

    The union over the novelty bands and both media types, plus the calibration order.
    A page nobody asks for costs a few kilobytes of fixture; a page somebody asks for
    and that is missing costs a live run.
    """
    queries: list[DiscoverQuery] = []
    for band in NOVELTY_BANDS.values():
        for kind in ("movie", "tv"):
            for page in band.pages:
                for order in ("popularity", "votes"):
                    queries.append(  # noqa: PERF401 - four nested dimensions read better flat
                        DiscoverQuery(
                            kind=kind,
                            language=dataset.language,
                            page=page,
                            order=order,
                            min_votes=band.min_votes,
                            max_votes=band.max_votes,
                        )
                    )
    return queries


async def record_cassette(paths: EvalPaths, *, confirmed: bool, out: TextIO) -> Cassette:
    """Record, from the real TMDb, the answers an offline replay of this vote set needs.

    ``eval fixtures`` rebuilds the generated set's cassette from its own catalogue,
    because that catalogue is invented and TMDb has never heard of it. A vote set of
    real titles has no such source: the answers come from TMDb once, and are then
    committed.

    The **whole retrieval surface** is recorded, not whatever one strategy happened to
    ask for: every title's details, every title's recommendations (any of them can be
    something this user liked) and every discovery page any novelty band reads. That way
    the file is a function of the vote set rather than of one run.

    What it cannot cover is the *details* of a title the real TMDb returns and this vote
    set has never heard of — which, with an open pool, is most cards. Those arrive with
    ``eval run --live --record``, one run per strategy, merged into the same file.
    """
    dataset = _dataset(paths.votes)
    out.write(_record_plan(dataset))
    _answered(confirmed=confirmed)
    api_key = _live_key()
    recorder = RecordingTransport(httpx2.AsyncHTTPTransport(), provider="tmdb")
    tmdb = TmdbMetadata(api_key, transport=recorder)
    try:
        await tmdb.genres()
        for entry in sorted(dataset.catalog, key=lambda row: row.ref):
            await tmdb.details(entry.ref, dataset.language)
            await tmdb.related(entry.ref, dataset.language)
        for query in _fixture_queries(dataset):
            await tmdb.discover(query)
    finally:
        await recorder.aclose()
    cassette = merge(_existing(paths.cassette), recorder.cassette)
    cassette.write(paths.cassette)
    out.write(f"{paths.cassette}: {len(cassette)} recorded answers\n")
    return cassette


def _record_plan(dataset: EvalDataset) -> str:
    return (
        "A live recording reaches real services. This one would call:\n"
        f"  TMDb        {len(dataset.catalog)} titles of '{dataset.name}', in "
        f"{dataset.language}: their details and their recommendations, plus the "
        "discovery pages a candidate pool is built from (a title with no translated "
        "overview costs a second call for the English one). TMDb's API is free for "
        "personal use; its rate limits and caching terms apply.\n"
        "  AI provider none.\n"
    )


def catalog_service(dataset: EvalDataset) -> httpx2.MockTransport:
    """Return TMDb as the fixture's own catalogue answers it.

    The fixture builder records through this, and a test drives a "live" run against it
    without reaching anything. A title the catalogue does not hold gets a 404, so the
    recording covers the catalogue and nothing else.

    It answers **three** endpoints since lot 4b: the details of one title, a page of
    ``/discover`` and a page of ``/{kind}/{id}/recommendations``. The generated vote set
    invents its titles and its ids, so the real TMDb has never heard of them: without
    these two the retrieval layer could not be replayed on it at all, and the only vote
    set the harness could exercise the engine against would be the real one. The
    answers are computed from the catalogue, deterministically, in TMDb's own shapes.
    """
    catalog = dataset.by_ref
    genres = _genre_ids(dataset)
    entries = sorted(dataset.catalog, key=lambda row: row.ref)

    def listing(parts: Sequence[str], params: Mapping[str, str]) -> httpx2.Response | None:
        """Answer the two endpoints that return a page of titles; ``None`` for neither."""
        if parts[0] == "discover" and len(parts) == _DETAILS_PARTS:
            kind = as_media_kind(parts[1])
            found = _discovered(entries, kind, params, genres) if kind is not None else None
            return _listing(found, params, genres) if found is not None else _not_found()
        if len(parts) == _RELATED_PARTS and parts[2] == "recommendations":
            seed = _entry(catalog, parts[0], parts[1])
            return _listing(_related(entries, seed), params, genres) if seed else _not_found()
        return None

    def handle(request: httpx2.Request) -> httpx2.Response:
        parts = request.url.path.removeprefix("/3/").split("/")
        params = dict(request.url.params.items())
        if parts[0] == "genre" and len(parts) == _RELATED_PARTS and parts[2] == "list":
            return _genre_list(genres)
        page = listing(parts, params)
        if page is not None:
            return page
        entry = _entry(catalog, parts[0], parts[1]) if len(parts) == _DETAILS_PARTS else None
        return httpx2.Response(200, json=_tmdb_payload(entry, genres)) if entry else _not_found()

    return httpx2.MockTransport(handle)


def _genre_list(genres: Mapping[str, int]) -> httpx2.Response:
    """TMDb's genre list, as the fixture's catalogue spells it."""
    rows = [{"id": index, "name": name} for name, index in sorted(genres.items())]
    return httpx2.Response(200, json={"genres": rows})


def _not_found() -> httpx2.Response:
    return httpx2.Response(404, json={"status_message": "The resource you requested."})


def _entry(
    catalog: Mapping[TitleRef, CatalogEntry], kind_text: str, id_text: str
) -> CatalogEntry | None:
    kind = as_media_kind(kind_text)
    if kind is None or not id_text.isdigit():
        return None
    return catalog.get(TitleRef(kind, int(id_text)))


def _genre_ids(dataset: EvalDataset) -> Mapping[str, int]:
    """One id per genre name, over the whole catalogue rather than per title.

    The ids have to be stable across entries: the retrieval layer filters on them, and
    a "Drama" that is 1000 in one row and 1001 in the next filters at random.
    """
    names = sorted({name for entry in dataset.catalog for name in entry.genres})
    return {name: 1000 + index for index, name in enumerate(names)}


def _discovered(
    entries: Sequence[CatalogEntry],
    kind: str,
    params: Mapping[str, str],
    genres: Mapping[str, int],
) -> list[CatalogEntry]:
    """Apply the filters TMDb's ``/discover`` applies, over the fixture's catalogue."""
    excluded = {
        int(value)
        for value in params.get("without_genres", "").split(",")
        if value.strip().isdigit()
    }
    kept = [
        entry
        for entry in entries
        if entry.kind == kind
        and (params.get("include_adult") == "true" or not entry.adult)
        and _at_least(entry.vote_count, params.get("vote_count.gte"))
        and _at_most(entry.vote_count, params.get("vote_count.lte"))
        and _at_least(entry.vote_average or 0.0, params.get("vote_average.gte"))
        and _after(entry, params)
        and not excluded.intersection(genres[name] for name in entry.genres)
    ]
    kept.sort(key=lambda entry: (-entry.popularity, entry.ref))
    return kept


def _related(entries: Sequence[CatalogEntry], seed: CatalogEntry) -> list[CatalogEntry]:
    """Stand in for ``/recommendations``: the same franchise first, then shared genres.

    A crude rule, deliberately. It has to be *some* fixed relation between invented
    titles so that a strategy reading the fixture's recommendations is reading something
    and not noise; it is not a claim about what TMDb would answer.
    """
    wanted = set(seed.genres)
    found = [
        entry
        for entry in entries
        if entry.ref != seed.ref
        and (
            (seed.franchise is not None and entry.franchise == seed.franchise)
            or wanted.intersection(entry.genres)
        )
    ]
    found.sort(
        key=lambda entry: (
            0 if seed.franchise is not None and entry.franchise == seed.franchise else 1,
            -entry.popularity,
            entry.ref,
        )
    )
    return found


def _at_least(value: float, bound: str | None) -> bool:
    return bound is None or value >= float(bound)


def _at_most(value: float, bound: str | None) -> bool:
    return bound is None or value <= float(bound)


def _after(entry: CatalogEntry, params: Mapping[str, str]) -> bool:
    field = "primary_release_date" if entry.kind == "movie" else "first_air_date"
    lower, upper = params.get(f"{field}.gte"), params.get(f"{field}.lte")
    if lower is not None and (entry.year is None or entry.year < int(lower[:4])):
        return False
    return not (upper is not None and (entry.year is None or entry.year > int(upper[:4])))


def _listing(
    found: Sequence[CatalogEntry], params: Mapping[str, str], genres: Mapping[str, int]
) -> httpx2.Response:
    """One page of twenty, in the envelope every TMDb list endpoint uses."""
    page = int(params.get("page", "1") or "1")
    start = (max(1, page) - 1) * _PAGE_SIZE
    rows = found[start : start + _PAGE_SIZE]
    return httpx2.Response(
        200,
        json={
            "page": page,
            "results": [_tmdb_row(entry, genres) for entry in rows],
            "total_pages": max(1, -(-len(found) // _PAGE_SIZE)),
            "total_results": len(found),
        },
    )


def _tmdb_row(entry: CatalogEntry, genres: Mapping[str, int]) -> dict[str, Any]:
    """One catalogue entry as a TMDb list result, which is not its detail shape."""
    date = f"{entry.year}-06-01" if entry.year else None
    row: dict[str, Any] = {
        "id": entry.tmdb_id,
        "overview": entry.overview,
        "genre_ids": [genres[name] for name in entry.genres],
        "poster_path": f"/{entry.tmdb_id}.jpg",
        "original_language": entry.original_language,
        "popularity": entry.popularity,
        "vote_average": entry.vote_average,
        "vote_count": entry.vote_count,
        "adult": entry.adult,
        "media_type": entry.kind,
    }
    if entry.kind == "movie":
        return row | {"title": entry.title, "original_title": entry.title, "release_date": date}
    return row | {"name": entry.title, "original_name": entry.title, "first_air_date": date}


def _tmdb_payload(entry: CatalogEntry, genres: Mapping[str, int]) -> dict[str, Any]:
    """One catalogue entry, in TMDb's own shape."""
    date = f"{entry.year}-06-01" if entry.year else None
    payload: dict[str, Any] = {
        "id": entry.tmdb_id,
        "overview": entry.overview,
        "genres": [{"id": genres[name], "name": name} for name in entry.genres],
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
