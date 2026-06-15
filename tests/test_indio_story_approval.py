"""Story approval flow: ✅ starts owner DM, vote_msg deleted but story_msg
stays in the channel. Owner sí/no processes the pending approval."""

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config as bot_config
import storyManager


class _FakeEmoji:
    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return self.name


def _fake_reaction_payload(user_id, message_id, emoji):
    return types.SimpleNamespace(
        user_id=user_id,
        message_id=message_id,
        emoji=_FakeEmoji(emoji),
    )


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(bot_config, "OWNER_ID", 999, raising=False)
    monkeypatch.setattr(
        bot_config, "INDIO_RELAY_URL", "http://fake-relay", raising=False
    )
    return bot_config


@pytest.fixture(autouse=True)
def clear_state():
    storyManager._pending_reviews.clear()
    storyManager._pending_owner_approvals.clear()
    storyManager._awaiting_first_msg.clear()
    storyManager._stories_today.clear()
    storyManager._last_story_at.clear()
    storyManager._messages_since_story.clear()


@pytest.fixture
def tmp_image(tmp_path, monkeypatch):
    import imagePool

    pool = tmp_path / "pool"
    pool.mkdir(parents=True)
    (pool / "test.jpg").write_text("fake-image-data")
    monkeypatch.setattr(imagePool, "POOL_DIR", str(pool), raising=False)
    return "pool/test.jpg"


@pytest.fixture
def channel():
    ch = MagicMock()
    ch.fetch_message = AsyncMock()
    ch.send = AsyncMock(return_value=MagicMock(id=999))
    return ch


@pytest.fixture
def bot(channel):
    b = MagicMock()
    b.user = types.SimpleNamespace(id=111)
    b.get_channel.return_value = channel
    return b, channel


def _seed_review(story_msg_id=1001, vote_msg_id=1002, guild_id=456):
    state = {
        "story_msg_id": story_msg_id,
        "vote_msg_id": vote_msg_id,
        "rel_path": "test.jpg",
        "story_text": "Un chiste de prueba.",
        "channel_id": 123,
        "guild_id": guild_id,
    }
    storyManager._pending_reviews[story_msg_id] = state
    storyManager._pending_reviews[vote_msg_id] = state
    storyManager._awaiting_first_msg[guild_id] = state
    return state


async def test_approve_deletes_vote_msg_only(cfg, bot, tmp_image):
    """✅ on the vote msg deletes only the vote message, story stays visible
    in the channel, and the owner approval state is set."""
    b, ch = bot
    state = _seed_review()

    vote_msg_mock = MagicMock()
    vote_msg_mock.delete = AsyncMock()
    ch.fetch_message.return_value = vote_msg_mock

    with patch.object(storyManager, "_relay_dm_file", AsyncMock(return_value=5001)):
        payload = _fake_reaction_payload(
            user_id=888, message_id=state["vote_msg_id"], emoji="✅"
        )
        await storyManager.handle_story_reaction(payload, b)

    # vote msg deleted
    ch.fetch_message.assert_awaited_with(state["vote_msg_id"])
    vote_msg_mock.delete.assert_awaited_once()

    # owner approval state set
    assert cfg.OWNER_ID in storyManager._pending_owner_approvals
    ctx = storyManager._pending_owner_approvals[cfg.OWNER_ID]
    assert ctx["rel_path"] == "test.jpg"
    assert ctx["story_text"] == "Un chiste de prueba."
    assert ctx["story_msg_id"] == 1001

    # story msg NOT in pending anymore
    assert 1001 not in storyManager._pending_reviews
    assert 1002 not in storyManager._pending_reviews


async def test_owner_yes_keeps_story_msg(cfg, bot):
    """Owner says 'sí' — image is saved and story_msg stays in channel."""
    b, ch = bot
    storyManager._pending_owner_approvals[cfg.OWNER_ID] = {
        "rel_path": "test.jpg",
        "story_text": "Un chiste de prueba.",
        "vote_msg_id": 1002,
        "story_msg_id": 1001,
        "channel_id": 123,
        "guild_id": 456,
    }

    with patch.object(
        storyManager, "_save_approved_story", AsyncMock(return_value="img-abc")
    ):
        reply = await storyManager.handle_owner_story_approval(cfg.OWNER_ID, "sí", b)

    assert "img-abc" in (reply or "")
    assert cfg.OWNER_ID not in storyManager._pending_owner_approvals

    # story_msg NOT deleted (no fetch_message call for story_msg)
    for call in ch.fetch_message.await_args_list:
        assert call[0][0] != 1001  # story_msg_id was NOT fetched


async def test_owner_no_deletes_story_msg(cfg, bot):
    """Owner says 'no' — story_msg is deleted, image stays in pool."""
    b, ch = bot
    storyManager._pending_owner_approvals[cfg.OWNER_ID] = {
        "rel_path": "test.jpg",
        "story_text": "Un chiste de prueba.",
        "vote_msg_id": 1002,
        "story_msg_id": 1001,
        "channel_id": 123,
        "guild_id": 456,
    }

    story_msg_mock = MagicMock()
    story_msg_mock.delete = AsyncMock()
    ch.fetch_message.return_value = story_msg_mock

    reply = await storyManager.handle_owner_story_approval(cfg.OWNER_ID, "no", b)

    assert "Descartada" in (reply or "")
    assert cfg.OWNER_ID not in storyManager._pending_owner_approvals

    # story_msg was fetched and deleted
    ch.fetch_message.assert_awaited_with(1001)
    story_msg_mock.delete.assert_awaited_once()


async def test_reject_deletes_both_messages(cfg, bot, tmp_image):
    """❌ in the review channel — both story_msg and vote_msg are deleted
    and daily limit is reset."""
    b, ch = bot
    state = _seed_review(guild_id=456)
    storyManager._stories_today[456] = 1
    storyManager._last_story_at[456] = 1000.0
    storyManager._messages_since_story[456] = 3

    msg_delete = AsyncMock()

    async def _fetch(mid):
        m = MagicMock()
        m.delete = msg_delete
        return m

    ch.fetch_message.side_effect = _fetch

    payload = _fake_reaction_payload(
        user_id=888, message_id=state["vote_msg_id"], emoji="❌"
    )
    await storyManager.handle_story_reaction(payload, b)

    # both deleted
    assert msg_delete.await_count == 2

    # daily state reset
    assert 456 not in storyManager._stories_today
    assert 456 not in storyManager._last_story_at
    assert 456 not in storyManager._messages_since_story


async def test_owner_bogus_text_keeps_pending(cfg, bot):
    """Owner says something else — pending approval stays, hint returned."""
    b, ch = bot
    storyManager._pending_owner_approvals[cfg.OWNER_ID] = {
        "rel_path": "test.jpg",
        "story_text": "...",
        "vote_msg_id": 1002,
        "story_msg_id": 1001,
        "channel_id": 123,
        "guild_id": 456,
    }

    reply = await storyManager.handle_owner_story_approval(
        cfg.OWNER_ID, "qué decís?", b
    )

    assert "sí" in (reply or "")
    assert cfg.OWNER_ID in storyManager._pending_owner_approvals  # still pending
