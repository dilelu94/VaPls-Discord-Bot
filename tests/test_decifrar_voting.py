"""Behavior: inline ASR-quality feedback on voice transcripts.

The bot samples 1-in-N voice transcripts and seeds 👍/❌ reactions on the
transcript message. The promises pinned here:

- When sampling fires, the bot seeds reactions on the transcript message.
- ❌ from any user appends a JSONL row to the false-positives log capturing
  the raw Whisper text + (optional) VOSK N-best.
- 👍 from any user does NOT touch the log.
- Either reaction triggers cleanup of the bot's seeded reactions.
- When the feature flag is off or the sampler doesn't fire, nothing is seeded
  and nothing is logged.

Tests speak only to ``record`` and ``handle_reaction_vote`` — the storage
layout is free to change.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_FEEDBACK_ENABLED", True)
    monkeypatch.setattr(main_config, "DECIFRAR_FEEDBACK_SAMPLE_RATE", 1)  # always sample
    monkeypatch.setattr(main_config, "DECIFRAR_FEEDBACK_TIMEOUT_MINUTES", 60)
    monkeypatch.setattr(main_config, "DECIFRAR_FALSE_POSITIVES_LOG_PATH",
                        str(tmp_path / "false_positives.jsonl"))
    decifrarVoting._reset_for_tests()
    yield
    decifrarVoting._reset_for_tests()


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _fake_bot(user_id: int = 999):
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = user_id
    return bot


# ---- record() ------------------------------------------------------------

async def test_record_seeds_reactions_when_sampler_fires(monkeypatch):
    import decifrarVoting

    seeded = []

    async def fake_seed(channel_id, message_id):
        seeded.append((channel_id, message_id))

    monkeypatch.setattr(decifrarVoting, "_seed_reactions", fake_seed)
    decifrarVoting._bot = _fake_bot()

    await decifrarVoting.record("hola indio", msg_id=111, channel_id=222)
    await asyncio.sleep(0)

    assert seeded == [(222, 111)]


async def test_record_is_a_noop_when_feature_disabled(monkeypatch):
    import decifrarVoting
    import config as main_config

    monkeypatch.setattr(main_config, "DECIFRAR_FEEDBACK_ENABLED", False)
    seeded = []

    async def fake_seed(channel_id, message_id):
        seeded.append((channel_id, message_id))

    monkeypatch.setattr(decifrarVoting, "_seed_reactions", fake_seed)

    await decifrarVoting.record("hola indio", msg_id=111, channel_id=222)
    await asyncio.sleep(0)

    assert seeded == []


async def test_record_is_a_noop_when_sampler_misses(monkeypatch):
    import decifrarVoting
    import config as main_config

    # Effectively impossible to fire: 1/very-big rolls 1.
    monkeypatch.setattr(main_config, "DECIFRAR_FEEDBACK_SAMPLE_RATE", 10_000_000)
    seeded = []

    async def fake_seed(channel_id, message_id):
        seeded.append((channel_id, message_id))

    monkeypatch.setattr(decifrarVoting, "_seed_reactions", fake_seed)

    await decifrarVoting.record("hola indio", msg_id=111, channel_id=222)
    await asyncio.sleep(0)

    assert seeded == []


async def test_record_ignores_calls_without_message_id(monkeypatch):
    import decifrarVoting

    seeded = []

    async def fake_seed(channel_id, message_id):
        seeded.append((channel_id, message_id))

    monkeypatch.setattr(decifrarVoting, "_seed_reactions", fake_seed)

    await decifrarVoting.record("hola", msg_id=None, channel_id=222)
    await decifrarVoting.record("hola", msg_id=111, channel_id=None)
    await decifrarVoting.record("", msg_id=111, channel_id=222)
    await asyncio.sleep(0)

    assert seeded == []


# ---- handle_reaction_vote() ----------------------------------------------

async def _seed_entry(decifrarVoting, msg_id, channel_id,
                     raw="ruido", vosk_result=None):
    """Drive the public record() path so the entry exists exactly like in
    production. The seeded reactions are stubbed to a no-op so the test
    doesn't need to fake Discord."""
    async def _noop(*args, **kwargs):
        return None

    # Monkey patch via attribute since we don't have monkeypatch here.
    decifrarVoting._seed_reactions = _noop  # type: ignore[attr-defined]
    await decifrarVoting.record(raw, msg_id=msg_id, channel_id=channel_id,
                                vosk_result=vosk_result)
    await asyncio.sleep(0)


async def test_x_reaction_logs_false_positive(monkeypatch):
    import decifrarVoting
    import config as main_config

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id,
                      raw="puro ruido", vosk_result={"alternatives": [{"text": "el indio"}]})

    bot = _fake_bot()
    # Stub the cleanup so we don't need real Discord.
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions",
                        AsyncMock())

    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="❌", user_id=42, added=True,
    )
    # Let the cleanup task settle.
    await asyncio.sleep(0)

    rows = _read_jsonl(main_config.DECIFRAR_FALSE_POSITIVES_LOG_PATH)
    assert len(rows) == 1
    assert rows[0]["raw_whisper"] == "puro ruido"
    assert rows[0]["voter_id"] == 42
    assert rows[0]["vosk_result"] == {"alternatives": [{"text": "el indio"}]}


async def test_thumbs_up_does_not_log(monkeypatch):
    import decifrarVoting
    import config as main_config

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id)

    bot = _fake_bot()
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions", AsyncMock())

    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="👍", user_id=42, added=True,
    )
    await asyncio.sleep(0)

    assert _read_jsonl(main_config.DECIFRAR_FALSE_POSITIVES_LOG_PATH) == []


async def test_any_reaction_triggers_cleanup(monkeypatch):
    import decifrarVoting

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id)

    bot = _fake_bot()
    cleanup_mock = AsyncMock()
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions", cleanup_mock)

    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="👍", user_id=42, added=True,
    )
    # cleanup runs as a task — give it a tick.
    await asyncio.sleep(0)

    cleanup_mock.assert_awaited()


async def test_unrelated_emoji_is_ignored(monkeypatch):
    import decifrarVoting
    import config as main_config

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id)

    bot = _fake_bot()
    cleanup_mock = AsyncMock()
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions", cleanup_mock)

    # An emoji we did NOT seed should not consume the entry.
    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="🎉", user_id=42, added=True,
    )
    await asyncio.sleep(0)

    cleanup_mock.assert_not_awaited()
    assert _read_jsonl(main_config.DECIFRAR_FALSE_POSITIVES_LOG_PATH) == []


async def test_bot_reactions_are_ignored(monkeypatch):
    """The bot itself seeds 👍/❌ — those events must not consume the entry."""
    import decifrarVoting
    import config as main_config

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id)

    bot = _fake_bot(user_id=999)
    cleanup_mock = AsyncMock()
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions", cleanup_mock)

    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="❌", user_id=999, added=True,
    )
    await asyncio.sleep(0)

    cleanup_mock.assert_not_awaited()
    assert _read_jsonl(main_config.DECIFRAR_FALSE_POSITIVES_LOG_PATH) == []


async def test_reaction_removes_are_ignored(monkeypatch):
    """We only care about reactions being ADDED, not removed."""
    import decifrarVoting
    import config as main_config

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id)

    bot = _fake_bot()
    cleanup_mock = AsyncMock()
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions", cleanup_mock)

    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="❌", user_id=42, added=False,
    )
    await asyncio.sleep(0)

    cleanup_mock.assert_not_awaited()
    assert _read_jsonl(main_config.DECIFRAR_FALSE_POSITIVES_LOG_PATH) == []


async def test_second_reaction_after_resolution_is_a_noop(monkeypatch):
    """Once a sample is resolved (a user reacted), additional reactions on
    the same message no longer log to the false-positive file."""
    import decifrarVoting
    import config as main_config

    msg_id, channel_id = 7777, 8888
    await _seed_entry(decifrarVoting, msg_id, channel_id, raw="primera")

    bot = _fake_bot()
    monkeypatch.setattr(decifrarVoting, "_clear_seeded_reactions", AsyncMock())

    # First user fires ❌ → row appended.
    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="❌", user_id=1, added=True,
    )
    # Second user fires ❌ on the same (now resolved) message → no extra row.
    await decifrarVoting.handle_reaction_vote(
        bot, channel_id=channel_id, message_id=msg_id,
        emoji="❌", user_id=2, added=True,
    )
    await asyncio.sleep(0)

    rows = _read_jsonl(main_config.DECIFRAR_FALSE_POSITIVES_LOG_PATH)
    assert len(rows) == 1
