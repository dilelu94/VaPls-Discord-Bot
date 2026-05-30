"""Per-guild idle watchdog that disconnects the bot after voice inactivity.

A background asyncio task polls the guild's ``voice_client`` and disconnects
when it has been neither playing nor paused for ``VOICE_IDLE_TIMEOUT_SECONDS``.

The watchdog is started and stopped by ``bot.on_voice_state_update`` when the
bot itself joins or leaves a voice channel, so callers that connect to voice
do not need to wire it up explicitly. ``/parar`` and ``/quit`` also call
``stop_idle_watchdog`` defensively before disconnecting.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord  # noqa: F401  # kept for type hints under `from __future__ import annotations`

import analytics
import config

logger = logging.getLogger("bot.idle_watchdog")

_DEFAULT_POLL_INTERVAL = 5.0

# guild_id -> running watchdog task
_watchdogs: dict[int, asyncio.Task] = {}


def _find_voice_client(bot, guild_id: int) -> Optional[discord.VoiceClient]:
    """Return the bot's VoiceClient for a guild, or None."""
    for vc in getattr(bot, "voice_clients", []) or []:
        guild = getattr(vc, "guild", None)
        if guild is not None and getattr(guild, "id", None) == guild_id:
            return vc
    return None


def _is_active(vc) -> bool:
    """Return True when the bot is producing audio or intentionally paused."""
    try:
        if vc.is_playing():
            return True
    except Exception:
        pass
    try:
        if vc.is_paused():
            return True
    except Exception:
        pass
    return False


async def _disconnect_idle(bot, guild_id: int) -> None:
    """Tear down the GuildPlayer (if any) and disconnect the voice client."""
    from playCommand import guildPlayers, clearGuildPlayer

    vc = _find_voice_client(bot, guild_id)
    channel = getattr(vc, "channel", None) if vc is not None else None
    channel_id = str(getattr(channel, "id", "") or "") if channel else ""
    channel_name = getattr(channel, "name", "") if channel else ""

    player = guildPlayers.get(guild_id)
    text_channel = getattr(player, "textChannel", None) if player is not None else None
    if text_channel is not None:
        try:
            await text_channel.send("👋 Desconectado por inactividad.")
        except Exception:
            logger.exception("idle watchdog: failed to post notice")

    if player is not None:
        try:
            clearGuildPlayer(guild_id)
        except Exception:
            logger.exception("idle watchdog: clearGuildPlayer failed")

    if vc is not None:
        try:
            await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                vc.cleanup()
            except Exception:
                pass
        except Exception:
            logger.exception("idle watchdog: disconnect failed")

    guild = bot.get_guild(guild_id) if hasattr(bot, "get_guild") else None
    try:
        analytics.capture(
            "voice channel left",
            guild=guild,
            properties={
                "channel_id": channel_id,
                "channel_name": channel_name,
                "trigger": "idle_timeout",
            },
        )
    except Exception:
        pass


async def _watch_loop(
    bot,
    guild_id: int,
    idle_timeout: float,
    poll_interval: float,
) -> None:
    """Poll the guild's voice client until it goes idle long enough to drop."""
    last_active = time.monotonic()
    try:
        while True:
            await asyncio.sleep(poll_interval)
            vc = _find_voice_client(bot, guild_id)
            if vc is None or not _is_connected(vc):
                return
            if _is_active(vc):
                last_active = time.monotonic()
                continue
            if time.monotonic() - last_active >= idle_timeout:
                logger.info(
                    "idle watchdog: guild=%s reached %.0fs of inactivity, disconnecting",
                    guild_id, idle_timeout,
                )
                await _disconnect_idle(bot, guild_id)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("idle watchdog: unexpected error (guild=%s)", guild_id)


def _is_connected(vc) -> bool:
    try:
        return bool(vc.is_connected())
    except Exception:
        return False


def start_idle_watchdog(
    bot,
    guild_id: int,
    *,
    idle_timeout: Optional[float] = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> asyncio.Task:
    """Start (or restart) the idle watchdog for a guild.

    Cancels any existing task for the same guild first so the timer always
    resets when the bot reconnects.
    """
    stop_idle_watchdog(guild_id)
    timeout = idle_timeout if idle_timeout is not None else config.VOICE_IDLE_TIMEOUT_SECONDS
    loop = asyncio.get_event_loop()
    task = loop.create_task(_watch_loop(bot, guild_id, timeout, poll_interval))
    _watchdogs[guild_id] = task
    logger.info(
        "idle watchdog: started for guild=%s (timeout=%.0fs, poll=%.2fs)",
        guild_id, timeout, poll_interval,
    )
    return task


def stop_idle_watchdog(guild_id: int) -> None:
    """Cancel the idle watchdog for a guild, if one is running."""
    task = _watchdogs.pop(guild_id, None)
    if task is None:
        return
    if not task.done():
        task.cancel()
        logger.info("idle watchdog: stopped for guild=%s", guild_id)
