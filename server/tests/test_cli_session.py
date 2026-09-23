"""``tindarr eval session``: the deck a person answers, driven without a person.

A session is live by definition, so the tests drive it through one transport that
answers as TMDb and as an OpenAI-compatible endpoint, and feed the keypresses down a
file object. What they are really about is the three promises the command makes: it says
what it will call before calling anything, every answer reaches the disk before the next
card is drawn, and the votes never leave a ``private/`` directory.
"""

import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.support.ai import COMPATIBLE_URL, body_of
from tindarr.main.evaluation import EvalError, catalog_service
from tindarr.main.session import (
    SessionOptions,
    read_key,
    run_session,
    session_plan,
)
from tindarr.swipe.evaluation import load_dataset
from tindarr.swipe.evaluation.session import ANSWERS, SessionStore, answered
from tindarr.swipe.evaluation.synthetic import build_synthetic_dataset

pytestmark = pytest.mark.anyio

SEED = 20260923
TMDB_HOST = "api.themoviedb.org"


class _Outside(httpx2.AsyncBaseTransport):
    """TMDb answering from an invented catalogue, and a model that picks from the pool.

    The model is not scripted with ids, because nobody knows them before the pool is
    built: it reads the candidate list out of the prompt it was sent and answers with the
    first few, which is what makes this a test of the loop and not of a fixture.
    """

    def __init__(self, tmdb: httpx2.MockTransport, *, picks: int = 3) -> None:
        self._tmdb = tmdb
        self._picks = picks
        self.prompts: list[str] = []
        #: What TMDb says carries each card, when a test asks for the providers at all.
        self.offers: list[dict[str, Any]] | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Route one request to whichever service answers at its host."""
        if request.url.host != TMDB_HOST:
            return self._completion(body_of(request))
        if self.offers is not None and request.url.path.endswith("/watch/providers"):
            return httpx2.Response(200, json={"results": {"US": {"flatrate": self.offers}}})
        return await self._tmdb.handle_async_request(request)

    async def aclose(self) -> None:
        """Nothing to close; a session closes only the transport it opened itself."""

    def _completion(self, body: Mapping[str, Any]) -> httpx2.Response:
        messages: Any = body.get("messages", [])
        prompt = "\n".join(str(row.get("content", "")) for row in messages)
        self.prompts.append(prompt)
        cards = [
            {
                "id": int(line.split("|")[0].removeprefix("- ").strip()),
                "media_type": "tv" if "(tv," in line else "movie",
                "rationale": "because of the one you liked",
                "pick_type": "safe",
            }
            for line in prompt.splitlines()
            if line.startswith("- ") and "|" in line
        ][: self._picks]
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "model-a",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps({"cards": cards})},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )


@pytest.fixture
def outside() -> _Outside:
    """TMDb and a model, behind one transport."""
    return _Outside(catalog_service(build_synthetic_dataset(SEED)))


@pytest.fixture(autouse=True)
def _endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two environment variables a live session refuses to run without."""
    monkeypatch.setenv("TINDARR_EVAL_TMDB_API_KEY", "tmdb-test-key")
    monkeypatch.setenv("TINDARR_EVAL_LLM_BASE_URL", COMPATIBLE_URL)
    monkeypatch.setenv("TINDARR_EVAL_LLM_MODEL", "model-a")


def options(tmp_path: Path, **rest: Any) -> SessionOptions:
    """A short session, writing somewhere a test may write."""
    settings: dict[str, Any] = {
        "out": tmp_path / "private" / "session" / "votes.json",
        "batches": 1,
        "batch_size": 3,
    }
    return SessionOptions(**(settings | rest))


def keystrokes(*keys: str) -> io.StringIO:
    """The keys somebody would press, one per line."""
    return io.StringIO("".join(f"{key}\n" for key in keys))


async def test_a_session_writes_every_answer_where_the_repository_cannot_see_it(
    tmp_path: Path, outside: _Outside
) -> None:
    out = io.StringIO()
    settings = options(tmp_path)

    report = await run_session(
        settings, confirmed=True, out=out, keys=keystrokes("l", "s", "d"), transport=outside
    )

    stored = load_dataset(settings.out)
    assert [row.vote for row in stored.users[0].votes] == ["like", "seen_liked", "dislike"]
    # Every card shown is described in the catalogue, so the replay can score it later.
    assert len(stored.catalog) == 3
    assert report.counts.scored == 3
    assert report.counts.likes == 1
    assert "1 of them you had already seen" in out.getvalue()


async def test_a_session_refuses_to_write_anywhere_but_a_private_directory(
    tmp_path: Path, outside: _Outside
) -> None:
    settings = options(tmp_path, out=tmp_path / "fixtures" / "votes.json")

    with pytest.raises(EvalError, match="private"):
        await run_session(settings, confirmed=True, out=io.StringIO(), transport=outside)


async def test_a_session_that_stops_keeps_its_votes_and_the_next_one_builds_on_them(
    tmp_path: Path, outside: _Outside
) -> None:
    settings = options(tmp_path, batches=2)
    first = io.StringIO()

    await run_session(
        settings, confirmed=True, out=first, keys=keystrokes("l", "q"), transport=outside
    )

    stored = load_dataset(settings.out)
    assert [row.vote for row in stored.users[0].votes] == ["like"]
    assert "Stopped." in first.getvalue()

    second = io.StringIO()
    report = await run_session(
        settings, confirmed=True, out=second, keys=keystrokes("d", "x", "l"), transport=outside
    )

    assert "1 vote(s) are already in it" in second.getvalue()
    resumed = load_dataset(settings.out)
    assert [row.vote for row in resumed.users[0].votes] == [
        "like",
        "dislike",
        "seen_disliked",
        "like",
    ]
    # The seq numbers carry on, and no title is offered twice across the two sessions.
    assert [row.seq for row in resumed.users[0].votes] == [0, 1, 2, 3]
    assert len({row.ref for row in resumed.users[0].votes}) == 4
    assert report.counts.scored == 3


async def test_a_session_ends_when_the_keys_run_out(tmp_path: Path, outside: _Outside) -> None:
    """An empty stream is somebody closing the terminal, which is a quit."""
    report = await run_session(
        options(tmp_path), confirmed=True, out=io.StringIO(), keys=io.StringIO(), transport=outside
    )

    assert report.counts.scored == 0


async def test_a_key_nobody_bound_is_not_somebody_s_opinion(
    tmp_path: Path, outside: _Outside
) -> None:
    out = io.StringIO()

    await run_session(
        options(tmp_path, batch_size=1),
        confirmed=True,
        out=out,
        keys=keystrokes("z", "l"),
        transport=outside,
    )

    assert "that key is not one of these" in out.getvalue()
    assert [row.vote for row in load_dataset(options(tmp_path).out).users[0].votes] == ["like"]


async def test_a_card_shows_what_somebody_needs_to_answer_it(
    tmp_path: Path, outside: _Outside
) -> None:
    out = io.StringIO()

    await run_session(
        options(tmp_path, batch_size=1),
        confirmed=True,
        out=out,
        keys=keystrokes("l"),
        transport=outside,
    )

    text = out.getvalue()
    assert "because of the one you liked" in text  # the model's sentence
    assert "/10 from" in text  # the rating and how many people gave it
    assert "[l] like" in text
    assert "[q] quit" in text


async def test_a_session_names_the_services_that_carry_a_card_when_tmdb_knows(
    tmp_path: Path, outside: _Outside
) -> None:
    """The region's offers, never "yours": this command knows no household's account."""
    outside.offers = [{"provider_id": 8, "provider_name": "A Service", "logo_path": "/a.jpg"}]
    out = io.StringIO()

    await run_session(
        options(tmp_path, batch_size=1, providers=True),
        confirmed=True,
        out=out,
        keys=keystrokes("l"),
        transport=outside,
    )

    assert "included in US: A Service" in out.getvalue()


async def test_a_private_vote_set_nobody_can_read_stops_the_session(
    tmp_path: Path, outside: _Outside
) -> None:
    settings = options(tmp_path)
    settings.out.parent.mkdir(parents=True)
    settings.out.write_text("{not json", encoding="utf-8")

    with pytest.raises(EvalError, match=r"votes\.json"):
        await run_session(settings, confirmed=True, out=io.StringIO(), transport=outside)


async def test_a_session_says_where_a_card_streams_only_when_it_was_asked_to(
    tmp_path: Path, outside: _Outside
) -> None:
    """One more TMDb request per card, and the answer is about the region, not the user."""
    plain = io.StringIO()
    await run_session(
        options(tmp_path, batch_size=1),
        confirmed=True,
        out=plain,
        keys=keystrokes("l"),
        transport=outside,
    )
    asked = io.StringIO()
    await run_session(
        options(tmp_path, batch_size=1, providers=True),
        confirmed=True,
        out=asked,
        keys=keystrokes("l"),
        transport=outside,
    )

    assert "included in" not in plain.getvalue()
    assert "where it streams" not in plain.getvalue()
    assert "its watch providers" in asked.getvalue()
    # This catalogue has no offers to report, and a TMDb that will not answer costs the
    # line rather than the card.
    assert "where it streams" in asked.getvalue()


def test_the_plan_says_what_it_will_call_and_where_the_votes_go(tmp_path: Path) -> None:
    text = session_plan(options(tmp_path, batches=2, batch_size=10), 0)

    assert "up to 36 requests" in text  # 20 cards, plus the pool's pages twice
    assert "up to 2 generations" in text
    assert COMPATIBLE_URL in text
    assert "stays out of the repository" in text


async def test_a_session_will_not_call_anything_it_has_not_been_allowed_to(
    tmp_path: Path, outside: _Outside
) -> None:
    """No terminal to ask at and no --yes is a session that calls nothing."""
    with pytest.raises(EvalError, match="--yes"):
        await run_session(options(tmp_path), out=io.StringIO(), transport=outside)

    assert not options(tmp_path).out.exists()


def test_a_key_is_read_as_a_key_or_as_a_line(tmp_path: Path) -> None:
    assert read_key(io.StringIO("lots\n")) == "l"
    # An empty line is a skip, so "just press enter" moves on.
    assert read_key(io.StringIO("\n")) == " "
    # A stream with nothing left in it is somebody who has gone.
    assert read_key(io.StringIO("")) == "q"


def test_every_key_the_legend_offers_is_one_the_loop_answers_to() -> None:
    assert answered("Q") == (True, None)
    assert answered("L") == (True, "like")
    assert answered("~") == (False, None)
    assert set(ANSWERS) == {"l", "d", "s", "x", " ", "q"}


def test_a_stored_session_is_the_same_shape_as_a_committed_fixture(tmp_path: Path) -> None:
    """So the harness can replay it, and so it could one day be merged into one."""
    path = tmp_path / "private" / "votes.json"
    store = SessionStore.open(
        path, name="mine", user_id="me", language="fr", region="FR", novelty="bold"
    )
    store.dataset.write(path)

    stored = load_dataset(path)
    assert stored.version == 1
    assert stored.language == "fr"
    assert stored.users[0].novelty == "bold"
