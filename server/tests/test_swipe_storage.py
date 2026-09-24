"""The rows the swipe engine writes: batches and cards, votes, jobs, profiles, usage.

These tests stay at the storage layer on purpose. The properties they check — a card is
only ever reachable by its owner, a vote survives its card, two claims cannot both win,
a daily cap cannot be spent twice — are the ones every layer above assumes without
checking, and they are cheapest to pin down here.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.ports.deck import VoteValue
from tindarr.ports.titles import TitleRef
from tindarr.storage import batches as batch_repository
from tindarr.storage import jobs as job_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import usage as usage_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.batches import CARD_LIFETIME, CARD_RETENTION, NewCard, StoredProvider
from tindarr.storage.db import write_transaction
from tindarr.storage.profiles import PreferencesPatch
from tindarr.storage.usage import DailyLimitReachedError, ProviderOption
from tindarr.storage.users import insert as insert_user
from tindarr.storage.users import new_user

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
FILM = TitleRef("movie", 27205)
SERIES = TitleRef("tv", 1399)


async def _user(engine: AsyncEngine, name: str = "alex") -> str:
    async with write_transaction(engine) as connection:
        user = await insert_user(
            connection, new_user(f"{name}-id", name, NOW, admin=False, remote=True)
        )
    return user.id


async def _batch(
    engine: AsyncEngine, user_id: str, *cards: NewCard, mood: str | None = None
) -> str:
    async with write_transaction(engine) as connection:
        batch_id = await batch_repository.create_batch(
            connection,
            user_id,
            mode="normal",
            novelty="balanced",
            media_filter="both",
            strategy="hybrid",
            mood=mood,
            now=NOW,
        )
        await batch_repository.store_cards(connection, batch_id, user_id, cards, now=NOW)
    return batch_id


def _card(ref: TitleRef = FILM, title: str = "Inception") -> NewCard:
    return NewCard(
        ref=ref,
        title=title,
        pick_type="explore",
        year=2010,
        genres=("Science Fiction",),
        poster_path="/poster.jpg",
        providers=(StoredProvider(provider_id=8, name="Netflix", offer="subscription"),),
        rationale="Because you liked Tenet.",
    )


# --- batches and cards ----------------------------------------------------------------


async def test_a_served_batch_hands_back_its_cards_and_starts_their_clock(
    engine: AsyncEngine,
) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card())
    async with write_transaction(engine) as connection:
        served = await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
    assert [card.ref for card in served] == [FILM]
    assert served[0].expires_at == NOW + CARD_LIFETIME
    assert served[0].providers[0].name == "Netflix"
    assert served[0].pick_type == "explore"


async def test_polling_twice_does_not_extend_a_card(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card())
    async with write_transaction(engine) as connection:
        await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
        again = await batch_repository.serve_batch(
            connection, batch_id, user_id, now=NOW + timedelta(hours=3)
        )
    assert again[0].expires_at == NOW + CARD_LIFETIME


async def test_a_card_is_only_reachable_by_its_owner(engine: AsyncEngine) -> None:
    owner = await _user(engine, "alex")
    other = await _user(engine, "robin")
    batch_id = await _batch(engine, owner, _card())
    async with write_transaction(engine) as connection:
        served = await batch_repository.serve_batch(connection, batch_id, owner, now=NOW)
        card_id = served[0].id
        assert await batch_repository.get_card(connection, owner, card_id) is not None
        assert await batch_repository.get_card(connection, other, card_id) is None
        assert await batch_repository.serve_batch(connection, batch_id, other, now=NOW) == []


async def test_an_expired_card_leaves_the_deck_but_stays_votable(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card())
    later = NOW + CARD_LIFETIME + timedelta(minutes=1)
    async with write_transaction(engine) as connection:
        served = await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
        pending = await batch_repository.list_pending(connection, user_id, "both", now=later)
        assert pending == []
        assert await batch_repository.get_card(connection, user_id, served[0].id) is not None


async def test_a_ready_batch_is_the_oldest_unserved_one(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    first = await _batch(engine, user_id, _card())
    await _batch(engine, user_id, _card(SERIES, "Game of Thrones"))
    async with engine.connect() as connection:
        ready = await batch_repository.ready_batch(connection, user_id, "both")
    assert ready is not None
    assert ready.id == first


async def test_an_empty_batch_is_never_offered_as_ready(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    await _batch(engine, user_id)
    async with engine.connect() as connection:
        assert await batch_repository.ready_batch(connection, user_id, "both") is None
        latest = await batch_repository.latest_batch(connection, user_id, "both")
    assert latest is not None
    assert latest.cards_count == 0


async def test_a_narrowed_filter_hides_the_other_kind_without_losing_the_batch(
    engine: AsyncEngine,
) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card(), _card(SERIES, "Dark"))
    async with write_transaction(engine) as connection:
        await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
        films = await batch_repository.list_pending(connection, user_id, "movie", now=NOW)
        both = await batch_repository.count_pending(connection, user_id, "both", now=NOW)
    assert [card.ref for card in films] == [FILM]
    assert both == 2


async def test_the_purge_keeps_voted_and_unserved_cards(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card(), _card(SERIES, "Dark"))
    await _batch(engine, user_id, _card(TitleRef("movie", 603), "The Matrix"))
    async with write_transaction(engine) as connection:
        served = await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=served[0].ref,
                value="like",
                card_id=served[0].id,
                pick_type=served[0].pick_type,
                title=served[0].title,
            ),
            voted_at=NOW,
            now=NOW,
        )
        gone = await batch_repository.purge(
            connection, now=NOW + CARD_RETENTION + timedelta(days=1)
        )
        left = await batch_repository.served_refs(connection, user_id)
    assert gone == 1
    assert left == {FILM}


async def test_a_stored_card_survives_a_row_an_operator_broke(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card())
    async with write_transaction(engine) as connection:
        served = await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
        await connection.exec_driver_sql(
            "UPDATE cards SET providers = ?, ratings = ?, genres = ?, trailer = ? WHERE id = ?",
            ("not json", "[]", '"nope"', "{}", served[0].id),
        )
        card = await batch_repository.get_card(connection, user_id, served[0].id)
    assert card is not None
    assert card.providers == ()
    assert card.genres == ()
    assert card.trailer is None
    assert card.ratings.imdb is None


# --- votes ------------------------------------------------------------------------------


async def test_a_vote_outlives_the_card_it_came_from(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    batch_id = await _batch(engine, user_id, _card())
    async with write_transaction(engine) as connection:
        served = await batch_repository.serve_batch(connection, batch_id, user_id, now=NOW)
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=FILM,
                value="like",
                card_id=served[0].id,
                pick_type="explore",
                title="Inception",
                year=2010,
            ),
            voted_at=NOW,
            now=NOW,
        )
        await connection.exec_driver_sql("DELETE FROM cards WHERE id = ?", (served[0].id,))
        stored = await vote_repository.get(connection, user_id, FILM)
    assert stored is not None
    assert stored.card_id is None
    assert stored.title == "Inception"
    assert stored.pick_type == "explore"


async def test_voting_again_replaces_the_answer_and_keeps_the_request(
    engine: AsyncEngine,
) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        values: tuple[VoteValue, ...] = ("like", "seen_disliked")
        for value in values:
            await vote_repository.record(
                connection,
                user_id,
                vote_repository.NewVote(
                    ref=FILM, value=value, card_id=None, pick_type="safe", title="Inception"
                ),
                voted_at=NOW,
                now=NOW,
            )
            if value == "like":
                await vote_repository.mark_requested(
                    connection, user_id, FILM, status="queued", now=NOW
                )
        stored = await vote_repository.get(connection, user_id, FILM)
        total = await vote_repository.count(connection, user_id)
    assert stored is not None
    assert stored.value == "seen_disliked"
    assert stored.requested is True
    assert total == 1


async def test_a_receipt_is_only_ever_written_once(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        assert await vote_repository.remember_receipt(connection, user_id, "q-1", now=NOW) is True
        assert await vote_repository.remember_receipt(connection, user_id, "q-1", now=NOW) is False
        seen = await vote_repository.seen_receipts(connection, user_id, ["q-1", "q-2"])
    assert seen == {"q-1"}


async def test_a_receipt_survives_the_undo_it_produced(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await vote_repository.remember_receipt(connection, user_id, "q-1", now=NOW)
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=FILM, value="like", card_id=None, pick_type="safe", title="Inception"
            ),
            voted_at=NOW,
            now=NOW,
        )
        assert await vote_repository.remove(connection, user_id, FILM) is True
        seen = await vote_repository.seen_receipts(connection, user_id, ["q-1"])
    assert seen == {"q-1"}


async def test_a_skip_leaves_the_cool_down_after_sixty_days(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=FILM, value="skip", card_id=None, pick_type="safe", title="Inception"
            ),
            voted_at=NOW,
            now=NOW,
        )
        inside = await vote_repository.skipped_refs(
            connection, user_id, now=NOW + timedelta(days=59)
        )
        outside = await vote_repository.skipped_refs(
            connection, user_id, now=NOW + timedelta(days=61)
        )
        counted = await vote_repository.count(connection, user_id)
    assert inside == {FILM}
    assert outside == frozenset()
    assert counted == 0


async def test_likes_are_only_like_votes_and_can_be_filtered(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        answers: tuple[tuple[TitleRef, VoteValue], ...] = (
            (FILM, "like"),
            (SERIES, "seen_liked"),
        )
        for ref, value in answers:
            await vote_repository.record(
                connection,
                user_id,
                vote_repository.NewVote(
                    ref=ref, value=value, card_id=None, pick_type="safe", title="A title"
                ),
                voted_at=NOW,
                now=NOW,
            )
        await vote_repository.mark_requested(connection, user_id, FILM, status="queued", now=NOW)
        all_likes = await vote_repository.list_likes(connection, user_id)
        waiting = await vote_repository.list_likes(connection, user_id, requested=False)
    assert [like.ref for like in all_likes] == [FILM]
    assert waiting == []


async def test_resetting_votes_leaves_the_profile_standing(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await profile_repository.save_profile(
            connection, user_id, "Loves: heists", user_edited=True, votes_at_update=1, now=NOW
        )
        await vote_repository.record(
            connection,
            user_id,
            vote_repository.NewVote(
                ref=FILM, value="like", card_id=None, pick_type="safe", title="Inception"
            ),
            voted_at=NOW,
            now=NOW,
        )
        deleted = await vote_repository.delete_all(connection, user_id)
        profile = await profile_repository.read_profile(connection, user_id)
    assert deleted == 1
    assert profile is not None
    assert profile.user_edited is True


# --- jobs ---------------------------------------------------------------------------------


async def test_only_one_job_of_a_kind_is_ever_claimed(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        first = await job_repository.claim(connection, user_id, "batch", now=NOW)
        second = await job_repository.claim(connection, user_id, "batch", now=NOW)
        other_kind = await job_repository.claim(connection, user_id, "profile", now=NOW)
    assert first is not None
    assert second is None
    assert other_kind is not None


async def test_a_finished_job_frees_the_next_one(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        first = await job_repository.claim(connection, user_id, "batch", now=NOW)
        assert first is not None
        await job_repository.finish(connection, first.id, result_id="b-1", now=NOW)
        second = await job_repository.claim(connection, user_id, "batch", now=NOW)
        finished = await job_repository.last_finished(connection, user_id, "batch")
    assert second is not None
    assert finished is not None
    assert finished.result_id == "b-1"


async def test_a_failure_is_reported_exactly_once(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        job = await job_repository.claim(connection, user_id, "batch", now=NOW)
        assert job is not None
        await job_repository.fail(connection, job.id, code="llm_unreachable", now=NOW)
        assert await job_repository.mark_reported(connection, job.id, now=NOW) is True
        assert await job_repository.mark_reported(connection, job.id, now=NOW) is False


async def test_a_restart_closes_the_job_that_held_a_user_s_lock(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        job = await job_repository.claim(connection, user_id, "batch", now=NOW)
        assert job is not None
        await job_repository.start(connection, job.id, now=NOW)
        abandoned = await job_repository.close_abandoned(connection, now=NOW)
        freed = await job_repository.claim(connection, user_id, "batch", now=NOW)
        closed = await job_repository.get(connection, job.id)
    assert [one.id for one in abandoned] == [job.id]
    assert freed is not None
    assert closed is not None
    assert closed.error_code == "interrupted"


async def test_finished_jobs_are_purged_and_running_ones_are_not(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        done = await job_repository.claim(connection, user_id, "batch", now=NOW)
        assert done is not None
        await job_repository.finish(connection, done.id, result_id=None, now=NOW)
        running = await job_repository.claim(connection, user_id, "profile", now=NOW)
        assert running is not None
        gone = await job_repository.purge(connection, now=NOW + timedelta(days=30))
        still_there = await job_repository.active_users(connection, "profile")
    assert gone == 1
    assert still_there == {user_id}


# --- profiles and preferences ---------------------------------------------------------


async def test_a_rewrite_never_clears_the_user_edited_flag(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await profile_repository.save_profile(
            connection, user_id, "mine", user_edited=True, votes_at_update=3, now=NOW
        )
        await profile_repository.save_profile(
            connection, user_id, "the model's", user_edited=False, votes_at_update=9, now=NOW
        )
        profile = await profile_repository.read_profile(connection, user_id)
    assert profile is not None
    assert profile.user_edited is True
    assert profile.text == "the model's"
    assert profile.votes_at_update == 9


async def test_a_refresh_error_is_recorded_without_inventing_a_profile(
    engine: AsyncEngine,
) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await profile_repository.set_refresh_error(connection, user_id, "llm_quota", now=NOW)
        failed = await profile_repository.read_profile(connection, user_id)
        await profile_repository.save_profile(
            connection, user_id, "written", user_edited=False, votes_at_update=1, now=NOW
        )
        fixed = await profile_repository.read_profile(connection, user_id)
    assert failed is not None
    assert failed.text == ""
    assert failed.refresh_error == "llm_quota"
    assert fixed is not None
    assert fixed.refresh_error is None


async def test_preferences_default_and_patch_one_field_at_a_time(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        assert (
            await profile_repository.read_preferences(connection, user_id)
        ).novelty == "balanced"
        await profile_repository.update_preferences(
            connection,
            user_id,
            PreferencesPatch(novelty="bold", given=frozenset({"novelty"})),
            now=NOW,
        )
        updated = await profile_repository.update_preferences(
            connection,
            user_id,
            PreferencesPatch(
                streaming_services=(8, 8, 337, -1), given=frozenset({"streaming_services"})
            ),
            now=NOW,
        )
    assert updated.novelty == "bold"
    assert updated.streaming_services == (8, 337)
    assert updated.media_filter == "both"


async def test_a_null_language_is_a_value_a_patch_can_set(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await profile_repository.update_preferences(
            connection,
            user_id,
            PreferencesPatch(language="fr-FR", given=frozenset({"language"})),
            now=NOW,
        )
        cleared = await profile_repository.update_preferences(
            connection,
            user_id,
            PreferencesPatch(language=None, given=frozenset({"language"})),
            now=NOW,
        )
    assert cleared.language is None


# --- usage ------------------------------------------------------------------------------


async def test_the_daily_cap_is_spent_before_the_provider_is_called(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        assert await usage_repository.reserve(connection, user_id, limit=2, now=NOW) == 1
        assert await usage_repository.reserve(connection, user_id, limit=2, now=NOW) == 0
        with pytest.raises(DailyLimitReachedError):
            await usage_repository.reserve(connection, user_id, limit=2, now=NOW)
        tomorrow = await usage_repository.reserve(
            connection, user_id, limit=2, now=NOW + timedelta(days=1)
        )
    assert tomorrow == 1


async def test_a_cap_of_zero_refuses_and_no_cap_never_does(engine: AsyncEngine) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        with pytest.raises(DailyLimitReachedError):
            await usage_repository.reserve(connection, user_id, limit=0, now=NOW)
        assert await usage_repository.reserve(connection, user_id, limit=None, now=NOW) == -1


async def test_tokens_and_failures_are_added_to_the_day_that_reserved(
    engine: AsyncEngine,
) -> None:
    user_id = await _user(engine)
    async with write_transaction(engine) as connection:
        await usage_repository.reserve(connection, user_id, limit=None, now=NOW)
        await usage_repository.record_tokens(
            connection, user_id, input_tokens=100, output_tokens=20, now=NOW
        )
        await usage_repository.record_tokens(
            connection, user_id, input_tokens=5, output_tokens=1, now=NOW
        )
        await usage_repository.record_failure(connection, user_id, now=NOW)
        rows = await usage_repository.list_usage(connection, since=NOW - timedelta(days=1))
        today = await usage_repository.usage_today(connection, now=NOW)
    assert len(rows) == 1
    assert (rows[0].generations, rows[0].input_tokens, rows[0].output_tokens) == (1, 105, 21)
    assert rows[0].failures == 1
    assert today == {user_id: 1}


async def test_the_region_cache_says_how_old_it_is_rather_than_hiding_it(
    engine: AsyncEngine,
) -> None:
    async with write_transaction(engine) as connection:
        await usage_repository.write_region_providers(
            connection,
            "FR",
            [ProviderOption(provider_id=8, name="Netflix", logo_path="/n.jpg")],
            now=NOW,
        )
        cached = await usage_repository.read_region_providers(connection, "FR")
    assert cached is not None
    assert [option.name for option in cached.providers] == ["Netflix"]
    assert cached.fresh(NOW + timedelta(days=6)) is True
    assert cached.fresh(NOW + timedelta(days=8)) is False


async def test_a_cached_region_an_operator_broke_reads_as_empty(engine: AsyncEngine) -> None:
    async with write_transaction(engine) as connection:
        await usage_repository.write_region_providers(
            connection, "FR", [ProviderOption(provider_id=8, name="Netflix")], now=NOW
        )
        await connection.exec_driver_sql(
            "UPDATE region_providers SET providers = ? WHERE region = ?",
            ('[{"provider_id": 0, "name": "x"}, "nope"]', "FR"),
        )
        cached = await usage_repository.read_region_providers(connection, "FR")
    assert cached is not None
    assert cached.providers == ()
