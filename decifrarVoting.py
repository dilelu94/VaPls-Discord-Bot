"""Inline ASR-quality feedback for voice transcripts.

For 1 out of every ``DECIFRAR_FEEDBACK_SAMPLE_RATE`` voice transcripts the
bot adds 👍 and ❌ reactions to the transcript message in the channel. Users
react inline:

  - 👍 ("entendiste bien") → no log, no action. Reactions are auto-cleared
    after ``DECIFRAR_FEEDBACK_TIMEOUT_MINUTES``.
  - ❌ ("no entendiste / wake-word falso positivo") → append a JSONL row to
    ``DECIFRAR_FALSE_POSITIVES_LOG_PATH`` capturing the raw whisper text and
    the optional VOSK N-best result, for offline debugging of ASR quality.

No persistent state, no voting channel, no in-memory cache promotion. The
JSONL is debug-only — readers parse it; nothing in the bot reads it back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Optional

import discord

import analytics
import config

logger = logging.getLogger("decifrarVoting")


# Reactions the bot seeds on sampled transcript messages.
_UP_EMOJI = "👍"
_FP_EMOJI = "❌"
_SEEDED_EMOJIS = (_UP_EMOJI, _FP_EMOJI)


# In-memory map of msg_id -> tracking entry. Wiped on restart by design; if
# nobody reacts within the timeout the sweeper clears the reactions and drops
# the entry.
_lock = asyncio.Lock()
_entries: dict[int, dict] = {}
_bot: Optional["discord.Bot"] = None
_started = False


# ---- Public entry point --------------------------------------------------


async def record(
    raw: str,
    msg_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    vosk_result: Optional[dict] = None,
) -> None:
    """Maybe seed reactions on a voice transcript for ASR-quality feedback.

    Safe to call fire-and-forget from any voice-transcript callsite. Only
    acts when ``DECIFRAR_FEEDBACK_ENABLED`` is true and the random sampler
    selects this message (``1 / DECIFRAR_FEEDBACK_SAMPLE_RATE``).
    """
    if not raw or msg_id is None or channel_id is None:
        return
    if not getattr(config, "DECIFRAR_FEEDBACK_ENABLED", True):
        return
    rate = max(1, int(getattr(config, "DECIFRAR_FEEDBACK_SAMPLE_RATE", 3)))
    if random.randint(1, rate) != 1:
        return

    async with _lock:
        _entries[msg_id] = {
            "ts": time.time(),
            "raw": raw,
            "channel_id": channel_id,
            "vosk_result": vosk_result,
            "resolved": False,
        }

    asyncio.create_task(_seed_reactions(channel_id, msg_id))
    analytics.capture(
        "decifrar feedback sampled",
        properties={
            "channel_id": channel_id,
            "message_id": msg_id,
        },
    )


async def _seed_reactions(channel_id: int, message_id: int) -> None:
    if _bot is None:
        return
    try:
        channel = _bot.get_channel(channel_id) or await _bot.fetch_channel(channel_id)
        msg = await channel.fetch_message(message_id)
        for emoji in _SEEDED_EMOJIS:
            await msg.add_reaction(emoji)
    except Exception:
        logger.exception(
            "decifrar_feedback: failed to seed reactions on %s", message_id
        )


# ---- Reaction handling ---------------------------------------------------


async def handle_reaction_vote(
    bot: discord.Bot,
    *,
    channel_id: int,
    message_id: int,
    emoji: str,
    user_id: int,
    added: bool,
) -> None:
    """Resolve a 👍/❌ reaction on a seeded transcript message.

    Ignores removes and any emoji we didn't seed. On ❌ logs a JSONL row;
    either way we clear the bot's reactions so the message stops looking
    pollable.
    """
    if not added:
        return
    if emoji not in _SEEDED_EMOJIS:
        return
    if bot.user is not None and user_id == bot.user.id:
        return

    async with _lock:
        entry = _entries.get(message_id)
        if entry is None or entry.get("resolved"):
            return
        entry["resolved"] = True

    if emoji == _FP_EMOJI:
        _log_false_positive(entry, voter_id=user_id)

    asyncio.create_task(_clear_seeded_reactions(bot, channel_id, message_id))
    async with _lock:
        _entries.pop(message_id, None)


def _log_false_positive(entry: dict, *, voter_id: int) -> None:
    path = getattr(
        config, "DECIFRAR_FALSE_POSITIVES_LOG_PATH", "data/false_positives.jsonl"
    )
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            logger.exception("decifrar_feedback: cannot create %s", parent)
            return
    record_row = {
        "ts": time.time(),
        "voter_id": voter_id,
        "raw_whisper": entry.get("raw"),
        "vosk_result": entry.get("vosk_result"),
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_row, ensure_ascii=False) + "\n")
        logger.info(
            "decifrar_feedback: logged false positive raw=%r",
            entry.get("raw", "")[:200],
        )
    except OSError:
        logger.exception("decifrar_feedback: failed to write false positive log")


async def _clear_seeded_reactions(
    bot: discord.Bot, channel_id: int, message_id: int
) -> None:
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await channel.fetch_message(message_id)
    except Exception as e:
        logger.warning(
            "Failed to fetch message %s for reaction clear: %s", message_id, e
        )
        analytics.capture_exception(
            e, properties={"action": "decifrar_fetch_msg", "message_id": message_id}
        )
        return
    for emoji in _SEEDED_EMOJIS:
        try:
            await msg.remove_reaction(emoji, bot.user)
        except Exception as e:
            logger.warning(
                "Failed to remove reaction %s on %s: %s", emoji, message_id, e
            )
            analytics.capture_exception(
                e, properties={"action": "decifrar_clear_reaction", "emoji": emoji}
            )


# ---- Startup + expiry ----------------------------------------------------


async def start(bot: "discord.Bot") -> None:
    """Idempotent startup hook: bind the bot reference and launch the expiry
    sweeper. Calling this when ``DECIFRAR_FEEDBACK_ENABLED`` is false binds
    the bot but skips the sweeper (no entries will ever be created)."""
    global _bot, _started
    if _started:
        return
    _bot = bot
    _started = True
    if not getattr(config, "DECIFRAR_FEEDBACK_ENABLED", True):
        return
    asyncio.create_task(_expiry_loop())
    logger.info(
        "decifrar_feedback started; sample_rate=%s",
        getattr(config, "DECIFRAR_FEEDBACK_SAMPLE_RATE", 3),
    )


async def _expiry_loop() -> None:
    """Periodic sweep — clear reactions on entries older than the timeout and
    drop them from memory."""
    while True:
        try:
            await asyncio.sleep(60)
            ttl_minutes = float(
                getattr(config, "DECIFRAR_FEEDBACK_TIMEOUT_MINUTES", 60)
            )
            ttl = ttl_minutes * 60
            if ttl <= 0:
                continue
            now = time.time()
            async with _lock:
                expired_ids = [
                    mid
                    for mid, e in _entries.items()
                    if not e.get("resolved") and (now - e.get("ts", now)) > ttl
                ]
                expired = [(mid, _entries.pop(mid)) for mid in expired_ids]
            if _bot is None:
                continue
            for mid, e in expired:
                await _clear_seeded_reactions(_bot, e.get("channel_id"), mid)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("decifrar_feedback: expiry loop error")


# ---- Test hook -----------------------------------------------------------


def _reset_for_tests() -> None:
    global _entries, _bot, _started
    _entries = {}
    _bot = None
    _started = False
