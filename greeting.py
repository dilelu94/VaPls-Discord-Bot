"""Greeting soundboard trigger helpers for voice channel joins."""
import asyncio
import logging
import os
import time

import discord

import config
from users import USERS

logger = logging.getLogger("greeting")

DEFAULT_GREETING = os.path.join("Audios", "Fish Carrot.m4a")

_last_greeting: dict[int, float] = {}
_pending_trigger_user: dict[int, int] = {}


def set_pending_trigger(channel_id: int, user_id: int) -> None:
    """Record the user that should be greeted for a channel join.

    Args:
        channel_id: Voice channel ID that will receive the greeting.
        user_id: Discord user ID to associate with the greeting sound.

    Returns:
        None.

    Side Effects:
        Stores pending trigger state in module-level memory.
    """
    _pending_trigger_user[channel_id] = user_id
    logger.info(f"[GREETING] pending trigger set: channel={channel_id} user={user_id}")


def _resolve_greeting_path(user_id):
    """Resolve the greeting audio path for a user.

    Args:
        user_id: Discord user ID, or None to use the default greeting.

    Returns:
        Absolute or relative path to the greeting audio file.
    """
    rel = USERS.get(user_id, {}).get("greeting") if user_id is not None else None
    if rel is None:
        rel = DEFAULT_GREETING
    return os.path.join(config.CUSTOM_AUDIO_PATH, rel)


async def trigger_soundboard_entry(channel):
    """Play the greeting audio for a channel if the throttle allows it.

    Args:
        channel: Discord voice channel where the bot is currently connected.

    Returns:
        None.

    Side Effects:
        Plays audio through the active voice client and updates throttle state.

    Async:
        This function is a coroutine and must be awaited or scheduled.
    """
    now = time.time()
    user_id = _pending_trigger_user.pop(channel.id, None)
    last = _last_greeting.get(channel.id, 0.0)
    logger.info(f"[GREETING] trigger fired: channel={channel.id} user={user_id} since_last={now - last:.1f}s")
    if now - last < 60.0:
        logger.info(f"[GREETING] throttled (< 60s since last greeting on this channel)")
        return
    _last_greeting[channel.id] = now
    try:
        await asyncio.sleep(2)
        vc = channel.guild.voice_client
        if not vc or not vc.is_connected():
            logger.info(f"[GREETING] skip: vc not connected (vc={vc})")
            return
        if vc.is_playing():
            logger.info(f"[GREETING] skip: vc already playing")
            return
        path = _resolve_greeting_path(user_id)
        if not os.path.exists(path):
            logger.warning(f"[GREETING] file missing: {path}")
            return
        logger.info(f"[GREETING] playing: {path}")
        vc.play(discord.FFmpegOpusAudio(path))
    except Exception as e:
        logger.exception(f"[GREETING] playback failed: {e}")
