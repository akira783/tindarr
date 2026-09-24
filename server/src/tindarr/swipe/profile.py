"""The taste profile: three short lists, rewritten from what somebody actually did.

It is the deck's memory in prose. ``StrategyContext.taste_profile`` goes into every
batch prompt, and it is what lets a batch reason about somebody's taste without being
handed two hundred votes — which is also why it is the one part of the engine whose
input is *titles*, not ids: a vote row carries the title the server copied off its own
card, so unlike the batch prompt this one can say "they liked Arrival" rather than
"movie 329865".

**What the user wrote is not the model's to rewrite.** It lives in its own column
(``tindarr.storage.profiles``) and is shown to the model as something to work *around*.
That makes the promise in the contract — "later AI rewrites keep this text and never
contradict it" — a property of the storage rather than a hope about a prompt: the worst
a badly behaved model can do is produce weak bullets underneath it.

**A rewrite is background work with a job row**, one per user at a time, debounced by
the vote count. The cost is the same as a batch's — one model call — so it is charged
the same way and shows up on the same usage page.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.llm import LlmUsage, Prompt
from tindarr.ports.media_server import Engagement
from tindarr.storage import jobs as job_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import usage as usage_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.profiles import MAX_PROFILE_CHARS, TasteProfile
from tindarr.storage.usage import DailyLimitReachedError
from tindarr.storage.users import User
from tindarr.storage.votes import VoteRecord
from tindarr.swipe.engine import Household, SwipeEngine
from tindarr.swipe.hybrid import PROMPT_ENGAGEMENT
from tindarr.swipe.votes import NEGATIVE_VOTES, POSITIVE_VOTES, SEEN_VOTES

__all__ = [
    "MAX_BULLETS",
    "PROFILE_INSTRUCTIONS",
    "ProfileService",
    "ProfileState",
    "TasteBullets",
    "profile_prompt",
    "render",
]

logger = logging.getLogger(__name__)

#: How many bullets each list may hold. Three short lists are a prompt section; ten are
#: a document that pushes the candidates out of the model's attention.
MAX_BULLETS: Final = 6
#: How long one bullet may be. Enough for a sentence, not for a paragraph.
MAX_BULLET_CHARS: Final = 200
#: How many votes the rewrite reasons over, newest first. Past this, a profile is being
#: written from a taste the person has moved on from.
PROFILE_VOTES: Final = 120
#: The fewest opinions worth writing a profile from. Under this it is a horoscope.
MIN_VOTES: Final = 5

PROFILE_INSTRUCTIONS: Final = (
    "You summarise somebody's taste in films and television from what they have said "
    "about specific titles, and only output raw JSON objects."
)


class TasteBullets(BaseModel):
    """The model's answer: three short lists, and nothing else.

    Untrusted output, like every other answer (the security model, section 6). Each
    bullet is bounded and only ever displayed as plain text or pasted into a later
    prompt; nothing here is looked up, fetched or executed.
    """

    model_config = ConfigDict(extra="forbid")

    loves: list[str] = Field(default_factory=list[str], max_length=MAX_BULLETS)
    avoids: list[str] = Field(default_factory=list[str], max_length=MAX_BULLETS)
    nuances: list[str] = Field(default_factory=list[str], max_length=MAX_BULLETS)


@dataclass(frozen=True, slots=True)
class ProfileState:
    """What ``GET /swipe/profile`` answers."""

    profile: TasteProfile | None
    votes: int
    refreshing: bool
    refresh_error: str | None = None


def render(bullets: TasteBullets) -> str:
    """Turn the model's three lists into the text a prompt reads.

    Headed lists rather than a paragraph, because the batch prompt passes this through
    verbatim and the shape is what makes it skimmable there. Empty lists are left out
    entirely: "Avoids:" with nothing under it reads as "they avoid nothing", which is a
    claim nobody made.
    """
    sections = (
        ("Loves", bullets.loves),
        ("Avoids", bullets.avoids),
        ("Nuances", bullets.nuances),
    )
    written = [
        "\n".join(
            [f"{heading}:", *(f"- {_line(item)}" for item in items[:MAX_BULLETS] if item.strip())]
        )
        for heading, items in sections
        if any(item.strip() for item in items)
    ]
    return "\n\n".join(written)[:MAX_PROFILE_CHARS]


def _line(text: str) -> str:
    """One bullet, flattened and bounded, so nothing can forge a heading of its own."""
    return " ".join(text.split())[:MAX_BULLET_CHARS]


class ProfileService:
    """Reading a profile, letting the user write one, and rewriting it in the background."""

    def __init__(self, engine: AsyncEngine, swipe: SwipeEngine, clock: Clock) -> None:
        self._engine = engine
        self._swipe = swipe
        self._clock = clock

    async def read(self, user: User) -> ProfileState:
        """Return the profile, whether a rewrite is running, and why the last one failed."""
        async with self._engine.connect() as connection:
            profile = await profile_repository.read_profile(connection, user.id)
            running = await job_repository.active(connection, user.id, "profile")
            votes = await vote_repository.count(connection, user.id)
        return ProfileState(
            profile=profile if profile is not None and profile.text else None,
            votes=votes,
            refreshing=running is not None,
            refresh_error=profile.refresh_error if profile is not None else None,
        )

    async def save(self, user: User, text: str) -> ProfileState:
        """Store what the user wrote about their own taste."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            votes = await vote_repository.count(connection, user.id)
            await profile_repository.save_user_text(
                connection, user.id, text.strip(), votes_at_update=votes, now=now
            )
        logger.info("a user edited their taste profile")
        return await self.read(user)

    async def claim(self, user: User) -> str | None:
        """Take this user's rewrite slot and charge the day, or return ``None``.

        A rewrite is a model call, so it is claimed and charged exactly as a batch is —
        one transaction, one slot, one generation off the daily cap. A household that
        would rather spend its budget on cards than on prose sets the cap and gets both
        behaviours from one number.
        """
        place = await self._swipe.household()
        limit = (
            user.daily_generation_limit
            if user.daily_generation_limit is not None
            else place.daily_generation_limit
        )
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            job = await job_repository.claim(connection, user.id, "profile", now=now)
            if job is None:
                return None
            try:
                await usage_repository.reserve(connection, user.id, limit=limit, now=now)
            except DailyLimitReachedError:
                await job_repository.fail(connection, job.id, code="daily_limit_reached", now=now)
                return None
        return job.id

    async def rewrite(self, user: User, job_id: str) -> bool:
        """Write this user's profile from their votes. Finishes the job whatever happens."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            await job_repository.start(connection, job_id, now=now)
        try:
            return await self._rewrite(user, job_id)
        except ProblemError as failure:
            await self._fail(user, job_id, failure.code)
            return False

    async def fail(self, user: User, job_id: str, code: str) -> None:
        """Close a rewrite that produced nothing."""
        await self._fail(user, job_id, code)

    async def _fail(self, user: User, job_id: str, code: str) -> None:
        """Close a rewrite that produced nothing, and stop it being asked for again.

        The debounce counter moves even though nothing was written. It counts *attempts*
        per ten opinions, not successes: leaving it where it was makes every later vote
        look like the tenth new one, so a provider that is down turns one swiping
        session into one paid rewrite per swipe.
        """
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            votes = await vote_repository.count(connection, user.id)
            await job_repository.fail(connection, job_id, code=code, now=now)
            await profile_repository.set_refresh_error(
                connection, user.id, code, votes_at_update=votes, now=now
            )
            await usage_repository.record_failure(connection, user.id, now=now)
        logger.info("a profile rewrite stopped early", extra={"reason": code})

    async def _rewrite(self, user: User, job_id: str) -> bool:
        llm = await self._swipe.llm()
        place = await self._swipe.household()
        async with self._engine.connect() as connection:
            stored = await vote_repository.list_for_user(connection, user.id)
            profile = await profile_repository.read_profile(connection, user.id)
            preferences = await profile_repository.read_preferences(connection, user.id)
        opinions = [vote for vote in stored if vote.value != "skip"]
        if len(opinions) < MIN_VOTES:
            # Not a failure: there is simply nothing to summarise yet, and a profile
            # invented from three swipes would steer every batch that follows.
            await self._finish(job_id, user, None, len(opinions))
            return False
        engagement = await self._swipe.engagement(user)
        answer = await llm.generate(
            Prompt(
                instructions=PROFILE_INSTRUCTIONS,
                message=profile_prompt(
                    opinions,
                    engagement,
                    place=place,
                    language=preferences.language or place.language,
                    written=profile.user_text if profile is not None else "",
                ),
            ),
            TasteBullets,
        )
        await self._finish(job_id, user, render(answer.value), len(opinions), answer.usage)
        logger.info("a taste profile was rewritten", extra={"votes": len(opinions)})
        return True

    async def _finish(
        self,
        job_id: str,
        user: User,
        text: str | None,
        votes: int,
        usage: LlmUsage | None = None,
    ) -> None:
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            if text is not None:
                await profile_repository.save_generated(
                    connection, user.id, text, votes_at_update=votes, now=now
                )
            else:
                await profile_repository.clear_refresh_error(connection, user.id)
            await job_repository.finish(connection, job_id, result_id=None, now=now)
            spent = usage or LlmUsage()
            await usage_repository.record_tokens(
                connection,
                user.id,
                input_tokens=spent.input_tokens,
                output_tokens=spent.output_tokens,
                now=now,
            )


def profile_prompt(
    votes: Sequence[VoteRecord],
    engagement: Sequence[Engagement],
    *,
    place: Household,
    language: str,
    written: str,
) -> str:
    """Build the rewrite's user message: what they said, what they watched, what they wrote.

    Deterministic top to bottom, like the batch prompt, so a recorded answer can be
    replayed. The vote lines carry titles because a vote row carries the title the
    server copied off its own card — the one place in the engine where that is true.
    """
    parts = [
        _OPENING,
        _vote_lines(votes),
        _engagement_lines(engagement),
        _written(written),
        _rules(language, place),
    ]
    return "\n\n".join(part for part in parts if part)


_OPENING: Final = (
    "Below is what one person said about films and series a recommendation deck showed "
    "them, and what they watched elsewhere. Summarise their taste."
)


def _vote_lines(votes: Sequence[VoteRecord]) -> str:
    """Quote the votes, newest first, in the words a reader of the deck would use."""
    recent = sorted(votes, key=lambda vote: vote.voted_at, reverse=True)[:PROFILE_VOTES]
    lines = "\n".join(f"- {_named(vote)}: {_verdict(vote)}" for vote in recent)
    return f"THEIR VERDICTS (newest first):\n{lines}" if lines else ""


def _named(vote: VoteRecord) -> str:
    year = f", {vote.year}" if vote.year else ""
    title = " ".join(vote.title.split()) or f"{vote.ref.kind} {vote.ref.tmdb_id}"
    return f"{title} ({vote.ref.kind}{year})"


def _verdict(vote: VoteRecord) -> str:
    seen = " (had already seen it)" if vote.value in SEEN_VOTES else ""
    if vote.value in POSITIVE_VOTES:
        return f"liked{seen}"
    return f"not for them{seen}" if vote.value in NEGATIVE_VOTES else "no opinion"


def _engagement_lines(engagement: Sequence[Engagement]) -> str:
    rows = [row for row in engagement if row.ref is not None][:PROMPT_ENGAGEMENT]
    lines = "\n".join(
        f"- {row.item.name} ({row.item.kind}): {row.state}" for row in rows if row.item.name
    )
    if not lines:
        return ""
    return (
        "WHAT THEY WATCHED OUTSIDE THE DECK, on their media server or in a history they "
        f"imported:\n{lines}\nA series they started and dropped is a mild negative; one "
        "they finished is a strong positive."
    )


def _written(written: str) -> str:
    """Quote what the user wrote about themselves, as something to work around.

    It is the one piece of this prompt a person typed, so it is where an injection would
    arrive; it is also the piece the answer must not contradict. Both are handled the
    same way — it is fenced, it is described as the user's own words, and the schema is
    what actually bounds the answer. Whatever the model does with it, the stored copy of
    this text is in another column and is not being rewritten.
    """
    text = "\n".join(line for line in written.splitlines() if line.strip())
    if not text:
        return ""
    return (
        "THE USER WROTE THIS ABOUT THEMSELVES, and it is not yours to change or "
        "contradict. It will be shown above your answer, so do not repeat it; say only "
        f"what their verdicts add to it:\n---\n{text[:MAX_PROFILE_CHARS]}\n---"
    )


def _rules(language: str, place: Household) -> str:
    region = f" They watch in {place.region}." if place.region else ""
    return (
        "Write three short lists about this person, in the language with ISO code "
        f'"{language}".{region}\n'
        f"- loves: up to {MAX_BULLETS} bullets on what to show them more of — genres, "
        "tones, eras, countries, kinds of story. Cite the evidence briefly.\n"
        f"- avoids: up to {MAX_BULLETS} bullets on what to stop showing them.\n"
        f"- nuances: up to {MAX_BULLETS} bullets on the conditions — what they like only "
        "sometimes, and when.\n"
        "One short sentence per bullet, concrete, about patterns rather than single "
        "titles. Say nothing you cannot point at in the verdicts above; leave a list "
        "empty rather than guess.\n\n"
        'Return ONLY a JSON object: {"loves": ["..."], "avoids": ["..."], "nuances": ["..."]}'
    )
