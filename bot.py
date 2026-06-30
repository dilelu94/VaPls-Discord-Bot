"""Main Discord bot entrypoint for VaPls.

Handles slash commands, voice playback, greeting triggers, analytics, and the
HTTP API server. Voice receive/transcription is delegated to the userbot in
./userbot/.
"""

import sys
import os
import json
import io
import logging
import asyncio
import re
import time
from urllib.parse import urljoin
import aiohttp
import discord
from discord.ext import commands

from playCommand import playLogic, openDjMenu
from pararCommand import pararLogic
from soundpadCommand import soundpadLogic, soundpad_query_autocomplete
from geminiCommand import vaplsLogic, indioLogic, SPACEWAR_GUIDE_TEXT
from suggestionsCommand import (
    sugerenciasLogic,
    sugerenciasVerLogic,
    migrate_existing_suggestions,
    sync_closed_issues,
)
import config
import analytics
import apiServer
from apiServer import startApiServer
import decifrarVoting
import errorHandler
import geminiKeys
import iptv
import decifrarVoting
from instagramCommand import start_instagram_reel_stream_logic, start_instagram_stream_logic
from idleWatchdog import start_idle_watchdog, stop_idle_watchdog
import huggingfaceImage
import transferCommand
from transferCommand import manager as transferManager
import storyManager
import petGenerator
# import geminiImage

# Voice receive / VOSK transcription moved to the userbot in ./userbot/.
# This bot is now output-only: it joins voice channels solely to play music,
# soundboard sounds, or chat greetings via /play and /soundpad. The userbot
# (a real Discord account) handles audio capture and Spanish transcription
# because DAVE (Discord's E2EE) does not give bots the MLS keys.

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(levelname)s:%(name)s: %(message)s",
)
log = logging.getLogger("bot")

import posthog_client

posthog_client.init_observability(service_name="vapls-main-bot")

if not discord.opus.is_loaded():
    for lib in ["libopus.so.0", "libopus.so", "opus"]:
        try:
            discord.opus.load_opus(lib)
            break
        except Exception:
            continue


async def safe_defer(ctx, ephemeral: bool = False):
    """Defer a Discord interaction if it has not been responded to yet.

    Args:
        ctx: Discord command context/interaction wrapper.
        ephemeral: If True, the deferred response (and all subsequent
            followups) are visible only to the invoker. Once an interaction
            is deferred public, followup ``ephemeral=True`` is silently
            ignored by Discord — pick the flag here.

    Returns:
        True if defer succeeded or was already done, False otherwise.

    Side Effects:
        Sends a deferred response via Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    if hasattr(ctx, "response") and ctx.response.is_done():
        return True
    try:
        await ctx.defer(ephemeral=ephemeral)
        return True
    except Exception:
        return False


async def safe_respond(ctx, message, ephemeral: bool = False, view=None):
    """Send a response or follow-up safely."""
    try:
        if ctx.response.is_done():
            return await ctx.followup.send(message, ephemeral=ephemeral, view=view)
        else:
            return await ctx.respond(message, ephemeral=ephemeral, view=view)
    except Exception:
        pass


async def safeEdit(ctx, message):
    """Edit the original response or fallback to responding.

    Args:
        ctx: Discord command context/interaction wrapper.
        message: Message content to send.

    Side Effects:
        Edits or sends a message via Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if ctx.response.is_done():
            await ctx.interaction.edit_original_response(content=message)
        else:
            await ctx.respond(message)
    except Exception:
        await safe_respond(ctx, message)


# ---- Activity/MMR relay helper -------------------------------------------
# The main bot logs Discord activities by POSTing to the userbot's relay
# endpoint, since the userbot owns the SQLite DB. Silently skipped when the
# relay URL is not configured.


async def _log_activity(
    user_id: int,
    guild_id: int,
    activity_type: str,
    *,
    channel_type: str = "",
    duration_secs: float = 0.0,
    quality_score: float | None = None,
    value: float = 1.0,
    metadata: dict | None = None,
    display_name: str = "",
):
    if not config.INDIO_RELAY_URL or not config.INDIO_RELAY_SECRET:
        return
    url = urljoin(config.INDIO_RELAY_URL, "/activity/log")
    payload = {
        "user_id": user_id,
        "guild_id": guild_id,
        "activity_type": activity_type,
        "channel_type": channel_type,
        "duration_secs": duration_secs,
        "value": value,
        "metadata": metadata or {},
        "display_name": display_name,
    }
    if quality_score is not None:
        payload["quality_score"] = quality_score
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3)
        ) as sess:
            async with sess.post(
                url,
                json=payload,
                headers={"X-API-Secret": config.INDIO_RELAY_SECRET},
            ) as resp:
                if resp.status >= 400:
                    log.warning(
                        f"[MMR] log_activity HTTP {resp.status}: "
                        f"{(await resp.text())[:100]}"
                    )
    except Exception as e:
        log.warning(f"[MMR] log_activity failed: {e}")


async def _fetch_pet_points(user_id: int, guild_id: int) -> dict:
    if not config.INDIO_RELAY_URL or not config.INDIO_RELAY_SECRET:
        return {"available": 0, "reserved": 0, "total_earned": 0, "spent": 0}
    url = urljoin(config.INDIO_RELAY_URL, f"/pet-points/{user_id}")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            async with sess.get(
                url,
                params={"guild_id": guild_id},
                headers={"X-API-Secret": config.INDIO_RELAY_SECRET},
            ) as resp:
                if resp.status >= 400:
                    return {"available": 0, "reserved": 0, "total_earned": 0, "spent": 0}
                return await resp.json()
    except Exception:
        return {"available": 0, "reserved": 0, "total_earned": 0, "spent": 0}


async def _post_pet_points(endpoint: str, user_id: int, guild_id: int, amount: float) -> bool:
    if not config.INDIO_RELAY_URL or not config.INDIO_RELAY_SECRET:
        return False
    url = urljoin(config.INDIO_RELAY_URL, endpoint)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            async with sess.post(
                url,
                json={"user_id": user_id, "guild_id": guild_id, "amount": amount},
                headers={"X-API-Secret": config.INDIO_RELAY_SECRET},
            ) as resp:
                return resp.status < 400
    except Exception:
        return False


async def _fetch_activity(endpoint: str, params: dict) -> dict | None:
    """Fetch data from a userbot relay activity endpoint (GET)."""
    if not config.INDIO_RELAY_URL or not config.INDIO_RELAY_SECRET:
        return None
    url = urljoin(config.INDIO_RELAY_URL, endpoint)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            async with sess.get(
                url,
                params=params,
                headers={"X-API-Secret": config.INDIO_RELAY_SECRET},
            ) as resp:
                if resp.status >= 400:
                    return None
                return await resp.json()
    except Exception:
        return None


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_USERBOT_ID = 519594605520486428


async def _classify_and_log_message(
    message,
    user_id: int,
    guild_id: int,
    content: str,
    channel_type: str,
    display_name: str = "",
):
    """Classify a guild message and log the appropriate activity to MMR."""
    if user_id == _USERBOT_ID:
        return

    tasks = []

    stickers = getattr(message, "stickers", None) or []
    if stickers:
        for _sticker in stickers:
            tasks.append(
                _log_activity(
                    user_id,
                    guild_id,
                    "sticker",
                    channel_type=channel_type,
                    display_name=display_name,
                )
            )

    attachments = getattr(message, "attachments", None) or []
    has_image = False
    has_file = False
    for a in attachments:
        ct = (a.content_type or "").lower()
        if ct.startswith("image/"):
            has_image = True
        else:
            has_file = True

    if has_image:
        tasks.append(
            _log_activity(
                user_id,
                guild_id,
                "image",
                channel_type=channel_type,
                display_name=display_name,
                quality_score=0.05 if not content else None,
            )
        )
    if has_file:
        tasks.append(
            _log_activity(
                user_id,
                guild_id,
                "file",
                channel_type=channel_type,
                display_name=display_name,
            )
        )

    poll = getattr(message, "poll", None)
    if poll is not None:
        tasks.append(
            _log_activity(
                user_id,
                guild_id,
                "poll_create",
                channel_type=channel_type,
                display_name=display_name,
            )
        )

    if content:
        lower = content.lower()
        if "tiktok.com" in lower:
            tasks.append(
                _log_activity(
                    user_id,
                    guild_id,
                    "tiktok_link",
                    channel_type=channel_type,
                    display_name=display_name,
                )
            )
        elif _URL_RE.search(content):
            tasks.append(
                _log_activity(
                    user_id,
                    guild_id,
                    "link",
                    channel_type=channel_type,
                    display_name=display_name,
                )
            )
        else:
            qs = None
            try:
                member = message.guild.get_member(user_id)
                if member and not member.voice:
                    qs = 0.05
            except Exception:
                pass
            tasks.append(
                _log_activity(
                    user_id,
                    guild_id,
                    "message",
                    channel_type=channel_type,
                    display_name=display_name,
                    quality_score=qs,
                )
            )

    # Thread/forum post bonus (on top of whatever activity the message is)
    if channel_type in ("public_thread", "private_thread"):
        parent = getattr(message.channel, "parent", None)
        if parent and getattr(parent, "type", None) is discord.ChannelType.forum:
            tasks.append(
                _log_activity(
                    user_id,
                    guild_id,
                    "forum_post",
                    channel_type=channel_type,
                    display_name=display_name,
                )
            )
        else:
            tasks.append(
                _log_activity(
                    user_id,
                    guild_id,
                    "thread_post",
                    channel_type=channel_type,
                    display_name=display_name,
                )
            )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


geminiKeys.load_from_disk()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
# Necesario para que on_message reciba DMs (handler que detecta API keys
# de Gemini cuando los users se las mandan al bot por privado).
intents.messages = True
intents.dm_messages = True
intents.message_content = True
# Necesario para contar votos por reacción en la votación de música del indio
# (on_raw_reaction_add). Está en Intents.default() pero lo dejamos explícito.
intents.reactions = True
# Sin esto, member.status siempre es "offline" y member.activities siempre
# vacío en /user/<id>. Requiere activar "PRESENCE INTENT" en el Developer Portal.
intents.presences = True
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
bot = discord.Bot(intents=intents)


@bot.event
async def on_connect():
    """Sync per-guild commands on connect for debug guilds.

    Async:
        This function is a coroutine and must be awaited by the Discord client.
    """
    log.info("Connected to Gateway. Starting command cleanup...")
    apiServer._GATEWAY_CONNECTED_AT = time.time()
    if config.DEBUG_GUILD_IDS:
        for guild_id in config.DEBUG_GUILD_IDS:
            try:
                await bot.sync_commands(guild_ids=[guild_id], force=True)
                log.info(f"Cleaned up local commands for guild {guild_id}")
            except Exception as e:
                log.warning(f"Error cleaning guild {guild_id}: {e}")
                analytics.capture_exception(
                    e, properties={"action": "sync_commands", "guild_id": guild_id}
                )
    log.info("Cleanup finished.")


_api_runner = None
_alert_listener = None


@bot.event
async def on_disconnect():
    """Log and alert when the bot disconnects from Discord Gateway."""
    log.warning("🔌 BOT DESCONECTADO del Gateway de Discord")


@bot.event
async def on_resume():
    """Log and alert when the bot reconnects to Discord Gateway."""
    log.warning("🔌 BOT RECONECTADO al Gateway de Discord")


@bot.event
async def on_ready():
    """Finalize startup tasks and launch the HTTP API server.

    Async:
        This function is a coroutine and must be awaited by the Discord client.
    """
    global _api_runner
    log.info(f"Bot online as {bot.user}")
    if _api_runner is None:
        try:
            _api_runner = await startApiServer(bot)
        except Exception as e:
            log.warning(f"Failed to start HTTP API: {e}")
    await bot.sync_commands()
    try:
        await decifrarVoting.start(bot)
    except Exception:
        log.exception("decifrar voting startup failed")

    # Auto-sync suggestions with GitHub Issues on startup.
    try:
        result = await migrate_existing_suggestions(dry_run=False)
        log.info("suggestions auto-migrate: %s", result)
    except Exception:
        log.exception("suggestions auto-migrate failed")
    try:
        result = await sync_closed_issues()
        log.info("suggestions auto-sync: %s", result)
    except Exception:
        log.exception("suggestions auto-sync failed")

    # Start file-transfer sweeper background task.
    try:
        asyncio.create_task(transferManager.start_sweeper())
        log.info("transfer sweeper started")
    except Exception:
        log.exception("transfer sweeper startup failed")

    # Start Indio story watcher (idle/voice triggers).
    try:
        asyncio.create_task(storyManager.start_story_watcher(bot))
        log.info("story watcher started")
    except Exception:
        log.exception("story watcher startup failed")

    # Start Israel alerts listener.
    global _alert_listener
    if config.ISRAEL_ALERTS_ENABLED and config.ISRAEL_ALERTS_CHANNEL_ID:
        try:
            from israel_alerts import IsraelAlertListener

            _alert_listener = IsraelAlertListener(bot, config.ISRAEL_ALERTS_CHANNEL_ID)
            asyncio.create_task(_alert_listener.start())
            log.info("israel alerts listener started")
        except Exception:
            log.exception("israel alerts listener startup failed")


# ---- Voice state tracking for MMR -----------------------------------------
# Per-guild per-user voice sessions with mute/deafen breakdown.
# AFK channel (451581345022476294) is excluded from tracking.
_AFK_CHANNEL_ID = 451581345022476294
_voice_sessions: dict[int, dict[int, dict]] = {}
# guild_id -> user_id -> {
#     "join_time": float,      # when user joined the channel
#     "channel_id": int,       # current channel
#     "state_since": float,    # when current mute/deafen state started
#     "state": str,            # "active" | "muted" | "deafened"
#     "active_secs": float,    # accumulated time in active state
#     "muted_secs": float,     # accumulated time muted (can still hear)
#     "deafened_secs": float,  # accumulated time deafened (implies muted)
# }
# Per-guild per-user watch-stream start timestamps.
_watch_start: dict[int, dict[int, float]] = {}
# Per-guild per-user cumulative watch-stream seconds today.
_watch_today: dict[int, dict[int, float]] = {}
_watch_date: str = ""
_WATCH_DAILY_MAX = 600.0  # 10 minutes
# Per-guild per-user daily stream start count (anti-farming cap).
_stream_today: dict[int, dict[int, int]] = {}
_stream_date: str = ""
_STREAM_DAILY_MAX = 5  # max stream starts per day that earn points


def _has_others(channel) -> bool:
    if channel is None:
        return False
    return (
        sum(1 for m in channel.members if not m.bot and m.id != config.USERBOT_USER_ID)
        >= 2
    )


def _streamers_in(channel) -> list[int]:
    if channel is None:
        return []
    return [
        m.id for m in channel.members if not m.bot and m.voice and m.voice.self_stream
    ]


def _finalize_watch(user_id: int, guild_id: int) -> None:
    global _watch_date, _watch_today
    guild_watch = _watch_start.get(guild_id, {})
    start = guild_watch.pop(user_id, None)
    if start is None:
        return
    today = time.strftime("%Y-%m-%d")
    if today != _watch_date:
        _watch_today.clear()
        _watch_date = today
    guild_today = _watch_today.setdefault(guild_id, {})
    used = guild_today.get(user_id, 0.0)
    remaining = max(0.0, _WATCH_DAILY_MAX - used)
    duration = min(time.time() - start, remaining)
    if duration > 0:
        guild_today[user_id] = used + duration
        asyncio.create_task(
            _log_activity(
                user_id, guild_id, "watch_stream", duration_secs=round(duration, 1)
            )
        )


def _voice_state_str(v):
    if v.self_deaf:
        return "deafened"
    if v.self_mute:
        return "muted"
    return "active"


def _start_voice_session(guild_id: int, user_id: int, channel, voice_state) -> None:
    now = time.time()
    s = _voice_state_str(voice_state)
    _voice_sessions.setdefault(guild_id, {})[user_id] = {
        "join_time": now,
        "channel_id": channel.id,
        "state_since": now,
        "state": s,
        "active_secs": 0.0,
        "muted_secs": 0.0,
        "deafened_secs": 0.0,
    }


def _finalize_voice_session(guild_id: int, user_id: int) -> None:
    guild_sessions = _voice_sessions.get(guild_id)
    if guild_sessions is None:
        return
    sess = guild_sessions.pop(user_id, None)
    if sess is None:
        return
    now = time.time()
    elapsed = now - sess["state_since"]
    sess[sess["state"] + "_secs"] += elapsed
    total = now - sess["join_time"]
    if total <= 0:
        return
    asyncio.create_task(
        _log_activity(
            user_id,
            guild_id,
            "voice_session",
            duration_secs=round(total, 1),
            metadata={
                "active_secs": round(sess["active_secs"], 1),
                "muted_secs": round(sess["muted_secs"], 1),
                "deafened_secs": round(sess["deafened_secs"], 1),
                "channel_id": sess["channel_id"],
            },
        )
    )


def _update_voice_state(guild_id: int, user_id: int, voice_state) -> None:
    guild_sessions = _voice_sessions.get(guild_id)
    if guild_sessions is None:
        return
    sess = guild_sessions.get(user_id)
    if sess is None:
        return
    new_state = _voice_state_str(voice_state)
    if new_state == sess["state"]:
        return
    now = time.time()
    elapsed = now - sess["state_since"]
    sess[sess["state"] + "_secs"] += elapsed
    sess["state"] = new_state
    sess["state_since"] = now


@bot.event
async def on_voice_state_update(member, before, after):
    """Track the bot's own voice state for analytics and greetings,
    and track ALL members' voice/camera/stream activities for MMR.

    Async:
        This function is a coroutine and must be awaited by the Discord client.
    """
    # Bot-only logic for analytics, greetings, idle watchdog
    if member == bot.user:
        if not before.channel and after.channel:
            analytics.capture(
                "voice channel joined",
                guild=after.channel.guild,
                properties={
                    "channel_id": str(after.channel.id),
                    "channel_name": after.channel.name,
                    "trigger": "state_update",
                },
            )
            try:
                start_idle_watchdog(bot, after.channel.guild.id)
            except Exception:
                log.exception("failed to start idle watchdog")
        elif before.channel and not after.channel:
            analytics.capture(
                "voice channel left",
                guild=before.channel.guild,
                properties={
                    "channel_id": str(before.channel.id),
                    "channel_name": before.channel.name,
                    "trigger": "state_update",
                },
            )
            try:
                stop_idle_watchdog(before.channel.guild.id)
            except Exception:
                log.exception("failed to stop idle watchdog")
            try:
                from playCommand import guildPlayers

                _player = guildPlayers.get(before.channel.guild.id)
                if _player is not None and _player.currentSong is not None:
                    _player.mark_interrupted()
                    _player._scheduleAutoResume(before.channel.id)
            except Exception:
                log.exception("failed to mark player as interrupted")
        return

    if member.bot:
        return

    guild_id = (after.channel or before.channel).guild.id

    n = member.display_name
    # ---- Voice session tracking (AFK channel excluded) ----
    if before.channel is None and after.channel is not None:
        if after.channel.id != _AFK_CHANNEL_ID:
            _start_voice_session(guild_id, member.id, after.channel, after)
        if _streamers_in(after.channel):
            _watch_start.setdefault(guild_id, {})[member.id] = time.time()
        if storyManager.check_voice_trigger(guild_id, after.channel):
            asyncio.create_task(
                storyManager.trigger_story(
                    bot,
                    guild_id,
                    config.INDIO_STORY_CHANNEL_ID,
                    trigger_type="voice",
                )
            )

    elif before.channel is not None and after.channel is None:
        _finalize_voice_session(guild_id, member.id)
        _finalize_watch(member.id, guild_id)

    elif (
        before.channel is not None
        and after.channel is not None
        and before.channel.id != after.channel.id
    ):
        if before.channel.id != _AFK_CHANNEL_ID:
            _finalize_voice_session(guild_id, member.id)
        if after.channel.id != _AFK_CHANNEL_ID:
            _start_voice_session(guild_id, member.id, after.channel, after)
        _finalize_watch(member.id, guild_id)
        if _streamers_in(after.channel):
            _watch_start.setdefault(guild_id, {})[member.id] = time.time()

    elif before.channel is not None and after.channel is not None:
        # Same channel — mute/deafen toggle
        _update_voice_state(guild_id, member.id, after)

    # ---- Camera tracking (quality scales with occupancy + other cameras) ----
    try:
        if not before.self_video and after.self_video and after.channel:
            if _has_others(after.channel):
                others = [
                    m
                    for m in after.channel.members
                    if not m.bot
                    and m.id != member.id
                    and m.id != config.USERBOT_USER_ID
                ]
                cam_count = sum(1 for m in others if m.voice and m.voice.self_video)
                q = min(1.0, 0.3 + 0.15 * len(others) + 0.2 * cam_count)
                asyncio.create_task(
                    _log_activity(
                        member.id,
                        guild_id,
                        "camera",
                        quality_score=round(q, 2),
                        display_name=n,
                    )
                )
    except Exception:
        pass

    # ---- Stream + watch_stream tracking (with viewer-based quality + daily cap) ----
    try:
        if not before.self_stream and after.self_stream and after.channel:
            viewers = sum(
                1 for m in after.channel.members if not m.bot and m.id != member.id
            )
            if viewers > 0:
                # Scale quality by viewer count: 1 viewer=0.4, 2=0.6, 3=0.8, 4+=1.0
                q = min(1.0, 0.2 + 0.2 * viewers)
                # Daily cap: only count first N stream starts per user
                today = time.strftime("%Y-%m-%d")
                if today != _stream_date:
                    _stream_today.clear()
                    _stream_date = today
                guild_stream = _stream_today.setdefault(guild_id, {})
                used = guild_stream.get(member.id, 0)
                if used < _STREAM_DAILY_MAX:
                    guild_stream[member.id] = used + 1
                    asyncio.create_task(
                        _log_activity(
                            member.id,
                            guild_id,
                            "stream",
                            quality_score=q,
                            display_name=n,
                        )
                    )
            # Existing channel members start watching this stream
            for m in after.channel.members:
                if not m.bot and m.id != member.id:
                    if m.id not in _watch_start.get(guild_id, {}):
                        _watch_start.setdefault(guild_id, {})[m.id] = time.time()

        if before.self_stream and not after.self_stream and before.channel:
            # Stream ended: finalize watch for current viewers
            for m in before.channel.members:
                if not m.bot and m.id != member.id:
                    _finalize_watch(m.id, guild_id)
    except Exception:
        pass


@bot.event
async def on_message(message):
    """Log activities from guild messages + DM handler for Gemini API keys.

    Guild messages are tracked for MMR: text, images, files, links, stickers,
    and polls. DMs containing Gemini API keys are forwarded to the key pool.
    """
    if message.author is None or message.author.bot:
        return

    if message.guild is not None:
        content = (message.content or "").strip()
        guild_id = message.guild.id
        user_id = message.author.id
        channel_type = str(getattr(message.channel, "type", "") or "")

        # Story system: track chat activity for idle detection
        storyManager.record_chat_activity(guild_id)
        # Catch first message after a story for context/feedback
        if message.channel.id == config.INDIO_STORY_CHANNEL_ID:
            asyncio.create_task(storyManager.handle_first_msg_after_story(message, bot))

        asyncio.create_task(
            _classify_and_log_message(
                message,
                user_id,
                guild_id,
                content,
                channel_type,
                display_name=message.author.display_name,
            )
        )
        return

    content = (message.content or "").strip()
    if not content:
        return
    found = geminiKeys.extract_keys_from_text(content)
    if not found:
        return
    owner_id = str(message.author.id)
    owner_name = getattr(message.author, "display_name", None) or getattr(
        message.author, "name", "unknown"
    )
    added: list[str] = []
    dupes: list[str] = []
    failed: list[tuple[str, str]] = []
    for k in found:
        ok, reason = await geminiKeys.add_key(
            k,
            owner_id=owner_id,
            owner_name=owner_name,
            source="dm:bot",
        )
        if ok:
            added.append(k)
        elif reason == "already in pool":
            dupes.append(k)
        else:
            failed.append((k, reason))
    lines: list[str] = []
    if added:
        lines.append(f"✅ Sumé {len(added)} key(s) al pool. ¡Gracias {owner_name}!")
    if dupes:
        lines.append(f"ℹ️ {len(dupes)} key(s) ya estaban cargadas.")
    if failed:
        lines.append(
            "❌ Algunas no pude sumarlas:\n" + "\n".join(f"- {r}" for _, r in failed)
        )
    if lines:
        try:
            await message.channel.send("\n".join(lines))
        except Exception:
            log.exception("on_message: reply failed")
    log.info(
        "gemini key DM from %s (%s): added=%d dupes=%d failed=%d",
        owner_name,
        owner_id,
        len(added),
        len(dupes),
        len(failed),
    )


@bot.event
async def on_raw_reaction_add(payload):
    """Route emoji reactions to the relevant subsystems + MMR tracking.

    Three consumers:
      - ``geminiCommand.register_reaction_vote``: counts keycap reactions on
        an open music-vote options message.
      - ``decifrarVoting.handle_reaction_vote``: resolves 👍/❌ on sampled
        voice-transcript messages (ASR-quality feedback).
      - MMR: logs reactions as activity, skipping decifrar voting reactions.
    """
    try:
        if bot.user is not None and payload.user_id == bot.user.id:
            return
        import geminiCommand

        geminiCommand.register_reaction_vote(
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            emoji=str(payload.emoji),
            user_id=payload.user_id,
        )
        import decifrarVoting

        await decifrarVoting.handle_reaction_vote(
            bot,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            emoji=str(payload.emoji),
            user_id=payload.user_id,
            added=True,
        )

        # Story system: ✅/❌ on review channel messages
        await storyManager.handle_story_reaction(payload, bot)

        # MMR: log reactions as activity, skipping decifrar voting reactions
        if payload.guild_id and not decifrarVoting.is_tracked(payload.message_id):
            asyncio.create_task(
                _log_activity(
                    payload.user_id,
                    payload.guild_id,
                    "reaction",
                    channel_type=str(payload.channel_id),
                )
            )
    except Exception:
        log.exception("on_raw_reaction_add failed")


@bot.event
async def on_thread_create(thread):
    """Track thread creation and posts for MMR."""
    try:
        if thread.guild is None:
            return
        guild_id = thread.guild.id
        owner_id = getattr(thread, "owner_id", None)
        if owner_id is None:
            return
        # Forum thread vs regular thread
        parent = getattr(thread, "parent", None)
        if parent and getattr(parent, "type", None) is discord.ChannelType.forum:
            act_type = "forum_create"
        else:
            act_type = "thread_create"
        asyncio.create_task(
            _log_activity(
                owner_id,
                guild_id,
                act_type,
                channel_type="thread",
            )
        )
    except Exception:
        log.exception("on_thread_create failed")


@bot.event
async def on_guild_channel_create(channel):
    """Track channel creation for MMR."""
    try:
        guild_id = getattr(channel, "guild", None)
        if guild_id is None:
            return
        guild_id = guild_id.id
        # We don't know who created it (Discord doesn't expose that in the
        # event), so we log it under guild_id as user_id 0 (system).
        asyncio.create_task(
            _log_activity(
                0, guild_id, "channel_create", channel_type=str(type(channel).__name__)
            )
        )
    except Exception:
        log.exception("on_guild_channel_create failed")


@bot.event
async def on_member_update(before, after):
    """Detect premium (boost) status changes for MMR."""
    try:
        guild_id = getattr(after, "guild", None)
        if guild_id is None:
            return
        guild_id = guild_id.id
        before_premium = getattr(before, "premium_since", None)
        after_premium = getattr(after, "premium_since", None)
        if bool(after_premium) != bool(before_premium):
            # Premium status changed; the userbot relay will be queried
            # by the /actividad command or the admin page. We don't set
            # premium in the DB here because the userbot owns the DB.
            log.info(
                "[MMR] premium changed for user %s in guild %s: %s → %s",
                after.id,
                guild_id,
                bool(before_premium),
                bool(after_premium),
            )
    except Exception:
        log.exception("on_member_update failed")


@bot.event
async def on_raw_poll_vote_add(payload):
    """Track poll votes for MMR."""
    try:
        if bot.user is not None and payload.user_id == bot.user.id:
            return
        guild_id = payload.guild_id
        if guild_id is None:
            return
        asyncio.create_task(_log_activity(payload.user_id, guild_id, "poll_vote"))
    except Exception:
        log.exception("on_raw_poll_vote_add failed")


@bot.event
async def on_guild_scheduled_event_create(event):
    """Track event creation for MMR."""
    try:
        guild_id = getattr(event, "guild_id", None)
        creator_id = getattr(event, "creator_id", None) or 0
        if guild_id is None:
            return
        asyncio.create_task(_log_activity(creator_id, guild_id, "event_create"))
    except Exception:
        log.exception("on_guild_scheduled_event_create failed")


@bot.event
async def on_guild_scheduled_event_subscribe(event, user_id):
    """Track event subscription (join) for MMR."""
    try:
        guild_id = getattr(event, "guild_id", None)
        if guild_id is None:
            return
        asyncio.create_task(_log_activity(user_id, guild_id, "event_join"))
    except Exception:
        log.exception("on_guild_scheduled_event_subscribe failed")


@bot.event
async def on_application_command(ctx):
    """Track slash command usage for MMR.

    Every command gets logged as ``slash_command`` activity. Quality is
    reduced when the user is not in a voice channel so in-voice commands
    contribute more than out-of-voice ones.
    """
    if ctx.author is None or ctx.author.bot:
        return
    if ctx.guild is None:
        return
    guild_id = ctx.guild.id
    user_id = ctx.author.id
    channel_type = str(getattr(ctx.channel, "type", "") or "")
    qs = None
    try:
        member = ctx.guild.get_member(user_id)
        if member and not member.voice:
            qs = 0.05
    except Exception:
        pass
    asyncio.create_task(
        _log_activity(
            user_id,
            guild_id,
            "slash_command",
            channel_type=channel_type,
            display_name=ctx.author.display_name,
            quality_score=qs,
        )
    )


@bot.event
async def on_application_command_error(ctx, error):
    """Red de seguridad para excepciones no atrapadas por los comandos.

    Los comandos atrapan sus propios errores con mensajes específicos
    (yt-dlp en playCommand, GeminiError en geminiCommand). Este handler
    solo se dispara cuando algo se escapa — evita que la interaction
    quede colgada en "thinking..." sin respuesta.
    """
    await errorHandler.handle(ctx, error)


def _track_command(ctx, name, extra=None):
    """Capture analytics for a slash command invocation.

    Args:
        ctx: Discord application context.
        name: Command name.
        extra: Optional dictionary of extra properties.

    Side Effects:
        Sends analytics events to PostHog when enabled.
    """
    analytics.identify_user(ctx.author)
    props = {"command": name, "channel_id": str(getattr(ctx.channel, "id", "") or "")}
    if extra:
        props.update(extra)
    analytics.capture(
        "command invoked", user=ctx.author, guild=ctx.guild, properties=props
    )


async def redirect_cmd(ctx, target_id: int | None) -> bool:
    """If the command was invoked outside its target channel, redirect.

    Returns True when the invocation was redirected (caller should return
    early). Sets up an ephemeral defer + notification so the user knows
    where the response went.
    """
    source_id = ctx.channel_id
    will = bool(target_id and source_id and source_id != target_id)
    if will:
        await safe_defer(ctx, ephemeral=True)
        try:
            await ctx.followup.send(f"➡️ Ejecutando en <#{target_id}>", ephemeral=True)
        except Exception:
            pass
    else:
        await safe_defer(ctx)
    return will


@bot.slash_command(
    name="dj", description="Abre el menú del modo DJ en el canal de música"
)
async def dj(ctx):
    """Slash command: activate Auto-DJ and post its control panel here.

    Activating in one step — /dj turns the mode on directly (no extra click)
    and posts the panel (veto / play-now / stop) in the channel where it was
    run. Refuses with a hint when nothing has played yet (cold start).

    Args:
        ctx: Discord application context.

    Async:
        This function is a coroutine and must be awaited.
    """
    if await redirect_cmd(ctx, config.INDIO_PLAY_CHANNEL_ID):
        return
    _track_command(ctx, "dj")
    if ctx.guild is None:
        await ctx.followup.send(
            "❌ Este comando solo funciona en un servidor.", ephemeral=True
        )
        return
    channel_id = getattr(ctx, "channel_id", None) or getattr(
        getattr(ctx, "channel", None), "id", None
    )
    ok, msg = await openDjMenu(ctx.bot, ctx.guild.id, channel_id)
    if ok:
        try:
            await ctx.followup.send("🎧 Modo DJ activado.", ephemeral=True)
        except Exception:
            pass
    elif msg == "cold-start":
        await ctx.followup.send(
            "🎧 Poné un tema primero (con /play) y después corré /dj.", ephemeral=True
        )
    else:
        await ctx.followup.send(f"❌ No pude activar el modo DJ: {msg}", ephemeral=True)


@bot.slash_command(name="parar")
async def parar(ctx):
    """Slash command: stop playback and disconnect.

    Args:
        ctx: Discord application context.

    Side Effects:
        Stops playback via pararLogic and disconnects voice if needed.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "parar")
    await pararLogic(ctx)


@bot.slash_command(name="queue", description="Muestra la cola de reproducción actual")
async def queueCommand(ctx):
    """Slash command: render the current queue as an ephemeral embed."""
    from playCommand import guildPlayers, build_queue_embed

    _track_command(ctx, "queue")
    player = guildPlayers.get(ctx.guild.id) if ctx.guild else None
    embed = build_queue_embed(player)
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(
    name="play", description="Reproduce una canción o playlist de YouTube"
)
async def play(
    ctx,
    query: discord.Option(
        str,
        description="Nombre de la canción o URL de YouTube",
        required=False,
        default=None,
    ) = None,
):
    """Slash command: queue and play a YouTube search or URL.

    Args:
        ctx: Discord application context.
        query: Search text or YouTube URL. If empty, replies with a hint
            instead of starting playback.

    Side Effects:
        Joins voice and starts the GuildPlayer playback flow.

    Async:
        This function is a coroutine and must be awaited.
    """
    will_redirect = (
        config.INDIO_PLAY_CHANNEL_ID and ctx.channel_id != config.INDIO_PLAY_CHANNEL_ID
    )
    await safe_defer(ctx, ephemeral=will_redirect)
    redirect_ch = None
    if will_redirect:
        try:
            await ctx.interaction.edit_original_response(
                content=f"musica en <#{config.INDIO_PLAY_CHANNEL_ID}>"
            )
        except Exception:
            pass
        if ctx.guild:
            ch = ctx.guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
            if ch is not None and hasattr(ch, "send"):
                redirect_ch = ch
    _track_command(ctx, "play", {"query_length": len(query or "")})
    if not query or not query.strip():
        await ctx.followup.send("decime qué reproducir la próxima", ephemeral=True)
        return
    await playLogic(ctx, query, redirect_channel=redirect_ch)


@bot.slash_command(
    name="soundpad", description="Abre el panel o reproduce un clip por nombre"
)
async def soundpad(
    ctx,
    query: discord.Option(
        str,
        description="Nombre aproximado del clip a reproducir (vacío = abrir panel)",
        required=False,
        default=None,
        autocomplete=soundpad_query_autocomplete,
    ) = None,
):
    """Slash command: open the soundpad UI or play a clip by fuzzy name.

    Args:
        ctx: Discord application context.
        query: Optional search string. When provided, the bot finds the
            closest-matching clip and plays it directly instead of opening
            the panel.

    Side Effects:
        Connects to voice and either sends an interactive view or plays a clip.

    Async:
        This function is a coroutine and must be awaited.
    """
    # Gate before defer: a synchronous in-memory check (geminiKeys.has_user_key)
    # is cheap enough to run inside the 3s interaction window, so users without
    # a donated key get an immediate ephemeral instead of seeing Discord's
    # "thinking…" first and the rejection second.
    if not geminiKeys.has_user_key(ctx.author.id):
        contributors = geminiKeys.format_contributors_line()
        msg = (
            "🔒 Para usar **/soundpad** necesitás aportar una API key de Gemini al pool del bot.\n\n"
            f"**Cómo conseguirla:** entrá a {config.GEMINI_KEYS_DONATION_URL}, "
            "clickeá *Create API key* (es gratis con una cuenta de Google) "
            "y mandámela por DM al bot. Apenas la sumo al pool podés usar el comando."
        )
        if contributors:
            msg = f"{msg}\n\n{contributors}"
        analytics.capture(
            "soundpad gated",
            user=ctx.author,
            guild=ctx.guild,
            properties={"reason": "no_user_key"},
        )
        await ctx.respond(msg, ephemeral=True)
        return

    will_redirect = (
        config.INDIO_PLAY_CHANNEL_ID and ctx.channel_id != config.INDIO_PLAY_CHANNEL_ID
    )
    await safe_defer(ctx, ephemeral=will_redirect)
    redirect_ch = None
    if will_redirect:
        try:
            await ctx.interaction.edit_original_response(
                content=f"soundpad en <#{config.INDIO_PLAY_CHANNEL_ID}>"
            )
        except Exception:
            pass
        if ctx.guild:
            ch = ctx.guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
            if ch is not None and hasattr(ch, "send"):
                redirect_ch = ch
    _track_command(ctx, "soundpad", {"query_length": len(query or "")})
    await soundpadLogic(ctx, query=query, redirect_channel=redirect_ch)


@bot.slash_command(name="vapls", description="consulta rápida, sin memoria")
async def vapls(ctx, pregunta: discord.Option(str, description="Tu pregunta")):
    """Slash command: ask the Gemini-backed VaPls persona.

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.

    Side Effects:
        Calls Gemini and sends the response back to Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "vapls", {"prompt_length": len(pregunta or "")})
    await vaplsLogic(ctx, pregunta)


@bot.slash_command(
    name="indio", description="integrante más del grupo, recuerda la charla"
)
async def indio(
    ctx,
    charla: discord.Option(str, description="Qué le decís al indio"),
):
    """Slash command: chat with the Indio persona (with history).

    Args:
        ctx: Discord application context.
        charla: User message to the Indio.

    Side Effects:
        Calls Gemini, updates history, and sends responses to Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    # Cuando el override de canal esta activo y el slash se invoca desde otro
    # canal, avisar al invocador ephemeral (solo lo ve el que disparó /indio)
    # que la respuesta va al target. El defer también va ephemeral, porque si
    # no Discord ignora el ephemeral=True del followup.
    source_id = getattr(ctx, "channel_id", None) or getattr(
        getattr(ctx, "channel", None), "id", None
    )
    target_id = config.INDIO_REPLY_CHANNEL_ID
    will_redirect = bool(target_id and source_id and source_id != target_id)
    await safe_defer(ctx, ephemeral=will_redirect)
    _track_command(ctx, "indio", {"prompt_length": len(charla or "")})
    if will_redirect:
        try:
            await ctx.followup.send(
                f"te respondo en <#{target_id}>",
                ephemeral=True,
            )
        except Exception:
            log.exception("indio: source-channel ack failed")
    await indioLogic(ctx, charla, False)


@bot.slash_command(
    name="generarimagen",
    description="Genera una imagen con Hugging Face (gratis, requiere token)",
)
async def generarimagen(
    ctx,
    prompt: discord.Option(
        str, description="Descripción de la imagen que querés generar"
    ),
):
    """Slash command: generate an image via Hugging Face Inference API.

    Args:
        ctx: Discord application context.
        prompt: Image description.

    Side Effects:
        Calls Hugging Face Inference API, sends the image to Discord,
        and deletes the temp file.

    Async:
        This function is a coroutine and must be awaited.
    """
    _track_command(ctx, "generarimagen", {"prompt_length": len(prompt or "")})
    await huggingfaceImage.generarimagenLogic(ctx, prompt)


# @bot.slash_command(
#     name="banana",
#     description="Genera una imagen con Gemini (gratis, sin API key, usando Playwright)",
# )
# async def banana(
#     ctx,
#     prompt: discord.Option(
#         str, description="Descripción de la imagen que querés generar"
#     ),
# ):
#     """Slash command: generate an image via Gemini web UI (browser automation).
#
#     Args:
#         ctx: Discord application context.
#         prompt: Image description.
#
#     Side Effects:
#         Launches a headless browser, generates the image, sends it to Discord,
#         and deletes the temp file.
#
#     Async:
#         This function is a coroutine and must be awaited.
#     """
#     _track_command(ctx, "banana", {"prompt_length": len(prompt or "")})
#     await geminiImage.bananaLogic(ctx, prompt)


@bot.slash_command(
    name="sugerencias",
    description="Sugerile algo al bot — se agrupa con ideas similares",
)
async def sugerencias(
    ctx,
    idea: discord.Option(str, description="Tu idea, cambio o feature deseado"),
):
    """Slash command: submit a free-form suggestion to the bot.

    Args:
        ctx: Discord application context.
        idea: User-provided suggestion text.

    Side Effects:
        Persists the suggestion to disk (grouped with similar prior ideas via
        Gemini Flash-Lite) and replies ephemerally to the user.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=True)
    except Exception as e:
        log.warning("sugerencias defer failed: %s", e)
    _track_command(ctx, "sugerencias", {"idea_length": len(idea or "")})
    await sugerenciasLogic(ctx, idea)


@bot.slash_command(
    name="sugerencias-ver",
    description="Mirá qué sugerencias ya existen, ordenadas por las más pedidas",
)
async def sugerencias_ver(ctx):
    """Slash command: list existing suggestion groups ranked by demand.

    Args:
        ctx: Discord application context.

    Side Effects:
        Reads the persisted suggestions and replies ephemerally with the
        ranked listing.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=True)
    except Exception as e:
        log.warning("sugerencias-ver defer failed: %s", e)
    _track_command(ctx, "sugerencias-ver", {})
    await sugerenciasVerLogic(ctx)


@bot.slash_command(name="quit", description="Sale del canal de voz")
async def quit(ctx):
    """Slash command: disconnect the bot from voice.

    Args:
        ctx: Discord application context.

    Side Effects:
        Disconnects the voice client and emits analytics.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "quit")

    vc = None
    for v in bot.voice_clients:
        if v.guild.id == ctx.guild.id:
            vc = v
            break

    if vc:
        channel_name = vc.channel.name
        channel_id = str(vc.channel.id)
        try:
            try:
                stop_idle_watchdog(ctx.guild.id)
            except Exception as e:
                log.warning("quit: stop_idle_watchdog failed: %s", e)
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    vc.cleanup()
                except Exception as e:
                    log.warning("quit: vc.cleanup failed: %s", e)
            analytics.capture(
                "voice channel left",
                user=ctx.author,
                guild=ctx.guild,
                properties={
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "trigger": "quit_command",
                },
            )
            try:
                await ctx.followup.send(
                    f"👋 Desconectado correctamente de {channel_name}."
                )
            except discord.NotFound:
                pass
        except Exception as e:
            analytics.capture_exception(
                e,
                user=ctx.author,
                guild=ctx.guild,
                properties={"action": "quit_disconnect"},
            )
            try:
                await ctx.followup.send(f"⚠️ Error al desconectar: {e}")
            except Exception:
                pass
    else:
        try:
            await ctx.followup.send("❌ No estoy conectado a voz en este servidor.")
        except Exception:
            pass



@bot.slash_command(
    name="entraindio", description="Hace que el indio entre a tu canal de voz"
)
async def entraindio(ctx):
    """Slash command: ask the userbot to join the caller's voice channel.

    Args:
        ctx: Discord application context.

    Side Effects:
        Sends an HTTP request to the userbot relay (``/join``) which makes
        the real-user Indio account connect to the caller's voice channel.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "entraindio")

    voice_state = getattr(ctx.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None) if voice_state else None
    if voice_channel is None:
        await safe_respond(
            ctx, "❌ Tenés que estar en un canal de voz para que el indio entre."
        )
        return

    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        await safe_respond(ctx, "❌ El relay del indio no está configurado.")
        return

    join_url = urljoin(config.INDIO_RELAY_URL, "/join")
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    payload = {"channel_id": int(voice_channel.id)}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(join_url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    log.warning("entraindio relay HTTP %s: %s", resp.status, body[:200])
                    await safe_respond(
                        ctx, f"⚠️ El indio no pudo entrar (HTTP {resp.status})."
                    )
                    return
    except Exception as e:
        log.exception("entraindio relay failed")
        await safe_respond(ctx, f"⚠️ Error llamando al indio: {e}")
        return

    await safe_respond(ctx, f"🪶 El indio va para **{voice_channel.name}**.")


async def start_iptv_stream_logic(
    guild_id: int,
    voice_channel: discord.VoiceChannel,
    stream_url: str,
    channel_name: str,
) -> tuple[bool, str, bool]:
    """Sends the HTTP request to the GoLive relay to start streaming a channel.

    Returns:
        (success, status_message, is_live)
    """
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        return False, "❌ El relay GoLive no está configurado.", True

    relay_base = config.GOLIVE_RELAY_URL
    url = urljoin(relay_base, "/stream")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {
        "guild_id": guild_id,
        "channel_id": voice_channel.id,
        "url": stream_url,
        "channel_name": channel_name,
    }
    log.info(
        "[STREAM_LOGIC] POST %s guild=%s channel=%s url=%s",
        url,
        guild_id,
        voice_channel.id,
        stream_url,
    )
    timeout = aiohttp.ClientTimeout(total=config.GOLIVE_RELAY_TIMEOUT)
    is_live = True
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning("stream relay HTTP %s: %s", resp.status, body[:200])
                    return False, f"⚠️ No pude iniciar el stream (HTTP {resp.status}).", True
                data = await resp.json()
                is_live = data.get("is_live", True)
    except Exception as e:
        log.exception("stream relay failed")
        return False, f"⚠️ Error iniciando stream: {e}", True

    return True, f"📺 Transmitiendo **{channel_name}** en **{voice_channel.name}**.\nUsá **/stopstream** para cortar.", is_live


async def _send_stream_control(guild_id: int, action: str, timestamp: float = 0.0) -> bool:
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        return False
    url = urljoin(config.GOLIVE_RELAY_URL, "/stream/control")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {"guild_id": guild_id, "action": action, "timestamp": timestamp}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                return resp.status < 400
    except Exception:
        return False


class StreamSeekModal(discord.ui.Modal):
    def __init__(self, guild_id: int):
        super().__init__(title="Saltar a un momento exacto")
        self.guild_id = guild_id
        self.add_item(
            discord.ui.InputText(
                label="Tiempo (MM:SS o segundos)",
                placeholder="Ej: 2:30 o 150",
                max_length=10,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        val = self.children[0].value.strip()
        try:
            if ":" in val:
                parts = val.split(":")
                sec = int(parts[0]) * 60 + int(parts[1])
            else:
                sec = int(val)
        except ValueError:
            await interaction.response.send_message("❌ Formato inválido.", ephemeral=True)
            return

        success = await _send_stream_control(self.guild_id, "seek", sec)
        if success:
            await interaction.response.send_message(f"⏩ Saltando al segundo {sec}...", ephemeral=True, delete_after=3)
        else:
            await interaction.response.send_message("❌ Error de comunicación con el stream.", ephemeral=True)


class StreamControlView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.is_paused = False
        self.message: discord.Message | None = None
        self._checker_task = asyncio.create_task(self._check_loop())

    async def _check_loop(self):
        try:
            await asyncio.sleep(5)
            while True:
                await asyncio.sleep(15)
                success = await _send_stream_control(self.guild_id, "status")
                if not success:
                    if self.message:
                        try:
                            await self.message.edit(view=None)
                        except Exception:
                            pass
                    self.stop()
                    break
        except asyncio.CancelledError:
            pass

    @discord.ui.button(label="⏸ Pausa / ▶ Reanudar", style=discord.ButtonStyle.primary, custom_id="stream_pause_toggle")
    async def toggle_pause(self, button: discord.ui.Button, interaction: discord.Interaction):
        action = "resume" if self.is_paused else "pause"
        success = await _send_stream_control(self.guild_id, action)
        if success:
            self.is_paused = not self.is_paused
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("❌ Error de comunicación con el stream.", ephemeral=True)

    @discord.ui.button(label="⏱️ Saltar a...", style=discord.ButtonStyle.secondary, custom_id="stream_seek")
    async def jump_to(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(StreamSeekModal(self.guild_id))

    @discord.ui.button(label="⏹ Detener", style=discord.ButtonStyle.danger, custom_id="stream_stop_btn")
    async def stop_stream_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message("⏹ Deteniendo el stream...", ephemeral=True, delete_after=3)
        ctx = type('Obj', (object,), {'guild_id': self.guild_id, 'respond': interaction.followup.send})()
        await stopstream(ctx)
        
        self.stop()
        if self._checker_task:
            self._checker_task.cancel()
            
        try:
            await interaction.edit_original_response(view=None)
        except Exception:
            pass


class IptvSearchModal(discord.ui.Modal):
    """Modal for free-text channel search within the IPTV browser."""

    def __init__(self, parent_view: "IptvSearchView"):
        super().__init__(title="🔍 Buscar canal por nombre")
        self.parent_view = parent_view
        self.add_item(
            discord.ui.InputText(
                label="Nombre del canal",
                placeholder="Ej: ESPN, Fox, CNN...",
                required=False,
                value=parent_view.search_query or "",
                max_length=100,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        query = self.children[0].value.strip() if self.children[0].value else ""
        self.parent_view.search_query = query or None
        self.parent_view.current_page = 0
        self.parent_view.selected_channel_idx = None
        self.parent_view.setup_components()
        await self.parent_view.update_message(interaction)


class IptvSearchView(discord.ui.View):
    """Interactive UI for browsing and filtering IPTV channels.

    Supports pagination (25 channels per page with ◀️ ▶️ buttons) and
    free-text search via a 🔍 modal to handle Discord's 25-option limit
    on select menus.
    """

    PAGE_SIZE = 25

    def __init__(self, channels: list[iptv.Channel], voice_channel: discord.VoiceChannel, redirect_ch=None):
        super().__init__(timeout=180)
        self.channels = channels
        self.voice_channel = voice_channel
        self.redirect_ch = redirect_ch
        self.selected_language = "es"
        self.selected_category = "all"
        self.selected_channel_idx: int | None = None
        self.search_query: str | None = None
        self.current_page = 0
        self.setup_components()

    def get_filtered_channels(self) -> list[iptv.Channel]:
        filtered = []
        for ch in self.channels:
            if self.selected_language != "all":
                if self.selected_language == "ar":
                    if ch.country != "AR":
                        continue
                elif self.selected_language == "ar2":
                    if ch.country != "AR2":
                        continue
                elif ch.language != self.selected_language:
                    continue
            if self.selected_category != "all":
                groups = [g.strip().lower() for g in ch.group.split(";")]
                if self.selected_category.lower() not in groups:
                    continue
            if self.search_query:
                if self.search_query.lower() not in ch.name.lower():
                    continue
            filtered.append(ch)
        filtered.sort(key=lambda x: x.name.lower())
        return filtered

    def _total_pages(self, total: int) -> int:
        return max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def setup_components(self):
        self.clear_items()

        # Row 0 — Language selector
        lang_options = [
            discord.SelectOption(label="🇦🇷 Argentina", value="ar", default=(self.selected_language == "ar")),
            discord.SelectOption(label="🇦🇷 Argentina 2", value="ar2", default=(self.selected_language == "ar2")),
            discord.SelectOption(label="🇪🇸 Español", value="es", default=(self.selected_language == "es")),
            discord.SelectOption(label="🇬🇧 Inglés", value="en", default=(self.selected_language == "en")),
            discord.SelectOption(label="🌐 Todos", value="all", default=(self.selected_language == "all")),
        ]
        lang_select = discord.ui.Select(
            placeholder="🌍 Idioma",
            options=lang_options,
            row=0,
            custom_id="iptv_lang_select",
        )
        lang_select.callback = self.on_lang_select
        self.add_item(lang_select)

        # Row 1 — Category selector
        cat_choices = [
            ("⚽ Deportes", "Sports"),
            ("📰 Noticias", "News"),
            ("🎬 Películas", "Movies"),
            ("🎵 Música", "Music"),
            ("🧪 Documentales", "Documentary"),
            ("🧸 Infantil", "Kids"),
            ("🎭 Entretenimiento", "Entertainment"),
            ("📺 Series", "Series"),
            ("🛐 Religión", "Religious"),
            ("🌐 Todos", "all"),
        ]
        cat_options = [
            discord.SelectOption(label=label, value=value, default=(self.selected_category == value))
            for label, value in cat_choices
        ]
        cat_select = discord.ui.Select(
            placeholder="📁 Categoría",
            options=cat_options,
            row=1,
            custom_id="iptv_cat_select",
        )
        cat_select.callback = self.on_cat_select
        self.add_item(cat_select)

        # Row 2 — Channel list (paged)
        filtered = self.get_filtered_channels()
        total_pages = self._total_pages(len(filtered))
        self.current_page = max(0, min(self.current_page, total_pages - 1))
        start = self.current_page * self.PAGE_SIZE
        page_channels = filtered[start : start + self.PAGE_SIZE]

        if page_channels:
            channel_options = []
            for i, ch in enumerate(page_channels):
                idx = start + i
                label = ch.name[:100]
                channel_options.append(
                    discord.SelectOption(
                        label=label,
                        value=str(idx),
                        default=(idx == self.selected_channel_idx),
                    )
                )
            page_label = f"Pág {self.current_page + 1}/{total_pages}" if total_pages > 1 else ""
            channel_select = discord.ui.Select(
                placeholder=f"📺 {len(filtered)} canales {page_label}".strip(),
                options=channel_options,
                row=2,
                custom_id="iptv_channel_select",
            )
            channel_select.callback = self.on_channel_select
            self.add_item(channel_select)
        else:
            self.add_item(
                discord.ui.Select(
                    placeholder="⚠️ No hay canales con este filtro",
                    options=[discord.SelectOption(label="Vacío", value="none")],
                    disabled=True,
                    row=2,
                    custom_id="iptv_channel_select",
                )
            )

        # Row 3 — Pagination buttons + search
        has_multiple_pages = total_pages > 1

        btn_prev = discord.ui.Button(
            emoji="◀️",
            style=discord.ButtonStyle.secondary,
            row=3,
            custom_id="iptv_prev",
            disabled=(not has_multiple_pages or self.current_page == 0),
        )
        btn_prev.callback = self.on_prev_page
        self.add_item(btn_prev)

        btn_page = discord.ui.Button(
            label=f"{self.current_page + 1}/{total_pages}",
            style=discord.ButtonStyle.secondary,
            row=3,
            custom_id="iptv_page_indicator",
            disabled=True,
        )
        self.add_item(btn_page)

        btn_next = discord.ui.Button(
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            row=3,
            custom_id="iptv_next",
            disabled=(not has_multiple_pages or self.current_page >= total_pages - 1),
        )
        btn_next.callback = self.on_next_page
        self.add_item(btn_next)

        search_label = "🔍" if not self.search_query else f"🔍 {self.search_query[:15]}"
        btn_search = discord.ui.Button(
            label=search_label,
            style=discord.ButtonStyle.primary if not self.search_query else discord.ButtonStyle.success,
            row=3,
            custom_id="iptv_search",
        )
        btn_search.callback = self.on_search
        self.add_item(btn_search)

        if self.search_query:
            btn_clear = discord.ui.Button(
                label="✕",
                style=discord.ButtonStyle.danger,
                row=3,
                custom_id="iptv_clear_search",
            )
            btn_clear.callback = self.on_clear_search
            self.add_item(btn_clear)

    def _build_embed(self, status_text: str = None) -> discord.Embed:
        embed = discord.Embed(
            title="📺 Buscador de Canales IPTV",
            description="Seleccioná idioma y categoría, luego elegí un canal para transmitir.",
            color=0xE94560,
        )
        lang_label = {"es": "🇪🇸 Español", "en": "🇬🇧 Inglés", "all": "🌐 Todos"}.get(
            self.selected_language, self.selected_language
        )
        embed.add_field(name="🌍 Idioma", value=lang_label, inline=True)

        cat_labels = {
            "Sports": "⚽ Deportes", "News": "📰 Noticias", "Movies": "🎬 Películas",
            "Music": "🎵 Música", "Documentary": "🧪 Documentales", "Kids": "🧸 Infantil",
            "Entertainment": "🎭 Entretenimiento", "Series": "📺 Series",
            "Religious": "🛐 Religión", "all": "🌐 Todos",
        }
        embed.add_field(
            name="📁 Categoría",
            value=cat_labels.get(self.selected_category, self.selected_category),
            inline=True,
        )

        filtered = self.get_filtered_channels()
        total_pages = self._total_pages(len(filtered))
        page_info = f" · pág {self.current_page + 1}/{total_pages}" if total_pages > 1 else ""
        embed.add_field(
            name="📊 Canales",
            value=f"{len(filtered)} encontrados{page_info}",
            inline=True,
        )

        if self.search_query:
            embed.add_field(name="🔍 Búsqueda", value=f'"{self.search_query}"', inline=True)

        if status_text:
            embed.add_field(name="⚡ Estado", value=status_text, inline=False)

        return embed

    async def update_message(self, interaction: discord.Interaction, status_text: str = None):
        embed = self._build_embed(status_text)
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            pass

    async def on_lang_select(self, interaction: discord.Interaction):
        self.selected_language = interaction.data["values"][0]
        self.selected_channel_idx = None
        self.current_page = 0
        self.setup_components()
        await self.update_message(interaction)

    async def on_cat_select(self, interaction: discord.Interaction):
        self.selected_category = interaction.data["values"][0]
        self.selected_channel_idx = None
        self.current_page = 0
        self.setup_components()
        await self.update_message(interaction)

    async def on_prev_page(self, interaction: discord.Interaction):
        self.current_page = max(0, self.current_page - 1)
        self.selected_channel_idx = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_next_page(self, interaction: discord.Interaction):
        filtered = self.get_filtered_channels()
        max_page = self._total_pages(len(filtered)) - 1
        self.current_page = min(max_page, self.current_page + 1)
        self.selected_channel_idx = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IptvSearchModal(self))

    async def on_clear_search(self, interaction: discord.Interaction):
        self.search_query = None
        self.current_page = 0
        self.selected_channel_idx = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_channel_select(self, interaction: discord.Interaction):
        selected_idx = int(interaction.data["values"][0])
        self.selected_channel_idx = selected_idx
        self.setup_components()

        filtered = self.get_filtered_channels()
        if selected_idx < 0 or selected_idx >= len(filtered):
            await interaction.response.send_message("❌ Error: Canal no encontrado.", ephemeral=True)
            return
        ch = filtered[selected_idx]

        voice_state = getattr(interaction.user, "voice", None)
        voice_channel = getattr(voice_state, "channel", None) if voice_state else None
        if voice_channel is None:
            await interaction.response.send_message("❌ Tenés que estar en un canal de voz para iniciar un stream.", ephemeral=True)
            return

        await interaction.response.defer()
        await self.update_message(interaction, status_text=f"🔄 Conectando y transmitiendo **{ch.name}**...")

        success, status_msg, _is_live = await start_iptv_stream_logic(
            interaction.guild_id,
            voice_channel,
            ch.url,
            ch.name
        )

        if success:
            view = StreamControlView(interaction.guild_id) if not _is_live else None
            if self.redirect_ch:
                msg = await self.redirect_ch.send(content=f"<@{interaction.user.id}> {status_msg}", view=view)
                if view:
                    view.message = msg
                await self.update_message(interaction, status_text=f"🟢 Transmisión iniciada en {voice_channel.name}")
            else:
                await interaction.edit_original_response(
                    content=f"🟢 {status_msg}", embed=None, view=view
                )
                if view:
                    try:
                        view.message = await interaction.original_response()
                    except Exception:
                        pass
        else:
            await self.update_message(interaction, status_text=f"🔴 Error: {status_msg}")


class IptvMultiSourceView(discord.ui.View):
    """Shows numbered buttons for multiple IPTV sources with the same name."""

    def __init__(self, results: list[iptv.Channel], voice_channel: discord.VoiceChannel, redirect_ch=None):
        super().__init__(timeout=60)
        self.results = results
        self.voice_channel = voice_channel
        self.redirect_ch = redirect_ch

        lang_emoji = {"es": "🇪🇸", "en": "🇺🇸"}
        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for i, ch in enumerate(results[:5]):
            label = ch.name
            if ch.group:
                label += f" ({ch.group})"
            label += f" {lang_emoji.get(ch.language, '🌐')}"
            btn = discord.ui.Button(
                label=f"{num_emojis[i]} {label}",
                style=discord.ButtonStyle.secondary,
                row=i // 3,
                custom_id=f"iptv_multi_{i}",
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            msg = self._last_interaction
            if msg:
                await msg.edit_original_response(view=self)
        except Exception:
            pass

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            ch = self.results[idx]
            self._last_interaction = interaction

            for child in self.children:
                child.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass

            voice_state = getattr(interaction.user, "voice", None)
            voice_channel = getattr(voice_state, "channel", None) if voice_state else None
            if voice_channel is None or voice_channel.id != self.voice_channel.id:
                await interaction.edit_original_response(
                    content="❌ Ya no estás en el canal de voz.", embed=None, view=None
                )
                return

            embed = discord.Embed(
                title="🔄 Iniciando transmisión...",
                description=f"**{ch.name}** en **{voice_channel.name}**",
                color=0xE94560,
            )
            await interaction.edit_original_response(embed=embed, view=None)

            success, status_msg, _is_live = await start_iptv_stream_logic(
                interaction.guild_id, self.voice_channel, ch.url, ch.name
            )

            if success:
                view = StreamControlView(interaction.guild_id) if not _is_live else None
                if self.redirect_ch:
                    msg = await self.redirect_ch.send(content=f"<@{interaction.user.id}> {status_msg}", view=view)
                    if view:
                        view.message = msg
                    await interaction.edit_original_response(
                        content=f"🟢 Transmisión iniciada en {voice_channel.name}", embed=None, view=None
                    )
                else:
                    await interaction.edit_original_response(
                        content=f"🟢 {status_msg}", embed=None, view=view
                    )
                    if view:
                        try:
                            view.message = await interaction.original_response()
                        except Exception:
                            pass
            else:
                await interaction.edit_original_response(
                    content=f"🔴 {status_msg}", embed=None, view=None
                )

        return callback


async def stream_autocomplete(ctx: discord.AutocompleteContext):
    query = ctx.value or ""
    try:
        return await iptv.search_autocomplete(query)
    except Exception:
        return []


@bot.slash_command(
    name="stream",
    description="Transmití un canal de IPTV en tu canal de voz (Go Live)",
)
async def stream(
    ctx,
    canal: discord.Option(
        str,
        description="Nombre del canal de IPTV (ej: ESPN, Fox, CNN)",
        required=False,
        default=None,
        autocomplete=stream_autocomplete,
    ),
):
    """Slash command: search iptv-org and start a Go Live stream.

    Args:
        ctx: Discord application context.
        canal: Search query for IPTV channel name.

    Side Effects:
        Searches the iptv-org M3U playlist, asks the userbot to join
        the caller's voice channel, and starts transcoding the M3U8
        stream with FFmpeg + libopenh264.
    """
    will_redirect = (
        config.INDIO_PLAY_CHANNEL_ID and ctx.channel_id != config.INDIO_PLAY_CHANNEL_ID
    )
    await safe_defer(ctx, ephemeral=will_redirect)
    redirect_ch = None
    if will_redirect:
        try:
            await ctx.interaction.edit_original_response(
                content=f"musica en <#{config.INDIO_PLAY_CHANNEL_ID}>"
            )
        except Exception:
            pass
        if ctx.guild:
            ch = ctx.guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
            if ch is not None and hasattr(ch, "send"):
                redirect_ch = ch

    _track_command(ctx, "stream", {"query_length": len(canal or "")})

    voice_state = getattr(ctx.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None) if voice_state else None
    if voice_channel is None:
        await safe_respond(
            ctx, "❌ Tenés que estar en un canal de voz para iniciar un stream."
        )
        return

    if canal is None:
        try:
            channels = await iptv.get_all_channels()
            if not channels:
                await safe_respond(ctx, "❌ No se pudieron cargar los canales de IPTV.")
                return
            view = IptvSearchView(channels, voice_channel, redirect_ch=redirect_ch)
            await ctx.interaction.edit_original_response(
                embed=discord.Embed(
                    title="📺 Buscador de Canales IPTV",
                    description="Seleccioná un idioma y categoría. Al elegir un canal, se iniciará la transmisión en tu canal de voz.",
                    color=0xE94560,
                ),
                view=view,
            )
            await view.update_message(ctx.interaction)
        except Exception as e:
            log.exception("failed to load IPTV search view")
            await safe_respond(ctx, f"⚠️ Error cargando el buscador: {e}")
        return

    is_url = canal.startswith(("http://", "https://", "rtsp://", "rtmp://"))

    # Instagram Reel detection — use yt-dlp extraction + vertical letterboxing
    if is_url and re.match(
        r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/",
        canal,
    ):
        log.info("[STREAM] Instagram reel detected: %s", canal[:80])
        success, status_msg = await start_instagram_reel_stream_logic(
            ctx.guild_id, voice_channel, canal,
        )
        if redirect_ch:
            await redirect_ch.send(content=f"<@{ctx.author.id}> {status_msg}")
        else:
            await safe_respond(ctx, status_msg)
        return

    if is_url:
        stream_url = canal
        channel_name = "Stream Directo"
    else:
        results = await iptv.search(canal, limit=5)
        if not results:
            await safe_respond(
                ctx,
                f'❌ No encontré canales de IPTV para "{canal}". Probá con otro nombre.',
            )
            return

        ch = results[0]
        if len(results) > 1:
            view = IptvMultiSourceView(results, voice_channel, redirect_ch=redirect_ch)
            embed = discord.Embed(
                title=f'📺 {len(results)} fuentes para "{canal}"',
                description="Elegí una fuente para transmitir:",
                color=0xE94560,
            )
            await ctx.interaction.edit_original_response(embed=embed, view=view)
            return
        stream_url = ch.url
        channel_name = ch.name

    log.info(
        "[STREAM] canal=%r channel=%s url=%s",
        canal,
        channel_name,
        stream_url,
    )

    success, status_msg, is_live = await start_iptv_stream_logic(
        ctx.guild_id,
        voice_channel,
        stream_url,
        channel_name
    )

    if success:
        view = StreamControlView(ctx.guild_id) if not is_live else None
        if redirect_ch:
            msg = await redirect_ch.send(content=f"<@{ctx.author.id}> {status_msg}", view=view)
            if view:
                view.message = msg
        else:
            await safe_respond(ctx, status_msg, view=view)
            if view:
                try:
                    view.message = await ctx.interaction.original_response()
                except Exception:
                    pass
    else:
        if redirect_ch:
            await redirect_ch.send(content=f"<@{ctx.author.id}> {status_msg}")
        else:
            await safe_respond(ctx, status_msg)


@bot.slash_command(
    name="stopstream",
    description="Detiene la transmisión de IPTV en curso",
)
async def stopstream(ctx):
    """Slash command: stop the active Go Live stream.

    Args:
        ctx: Discord application context.

    Side Effects:
        POSTs to the userbot relay ``/stopstream`` which kills FFmpeg
        and cleans up the video RTP session.
    """
    await safe_defer(ctx)
    _track_command(ctx, "stopstream")

    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        await safe_respond(ctx, "❌ El relay GoLive no está configurado.")
        return

    url = urljoin(config.GOLIVE_RELAY_URL, "/stopstream")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {"guild_id": ctx.guild_id}
    timeout = aiohttp.ClientTimeout(total=config.GOLIVE_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status == 404:
                    await safe_respond(
                        ctx, "❌ No hay ningún stream activo en este servidor."
                    )
                    return
                if resp.status >= 400:
                    log.warning("stopstream relay HTTP %s: %s", resp.status, body[:200])
                    await safe_respond(
                        ctx, f"⚠️ No pude detener el stream (HTTP {resp.status})."
                    )
                    return
    except Exception as e:
        log.exception("stopstream relay failed")
        await safe_respond(ctx, f"⚠️ Error deteniendo stream: {e}")
        return

    await safe_respond(ctx, "🛑 Stream detenido.")


@bot.slash_command(
    name="instagram",
    description="Transmití Reels de Instagram en tu canal de voz (Go Live, infinito)",
)
async def instagram(ctx):
    """Slash command: start infinite Instagram Reel streaming.

    Uses yt-dlp to discover reel URLs from an Instagram source page
    (configurable via INSTAGRAM_REEL_SOURCE in golive/.env, defaults to
    explore/tags/reels).  Each reel is extracted via yt-dlp for proper
    video+audio DASH streams with vertical letterboxing.  No Instagram
    credentials required — the shared cookies.txt handles auth if needed.
    Use /stopstream to end.

    Args:
        ctx: Discord application context.

    Side Effects:
        Joins voice, POSTs to the GoLive relay, and begins streaming
        Instagram Reels via GoLive.
    """
    will_redirect = (
        config.INDIO_PLAY_CHANNEL_ID and ctx.channel_id != config.INDIO_PLAY_CHANNEL_ID
    )
    await safe_defer(ctx, ephemeral=will_redirect)
    redirect_ch = None
    if will_redirect:
        try:
            await ctx.interaction.edit_original_response(
                content=f"instagram en <#{config.INDIO_PLAY_CHANNEL_ID}>"
            )
        except Exception:
            pass
        if ctx.guild:
            ch = ctx.guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
            if ch is not None and hasattr(ch, "send"):
                redirect_ch = ch

    _track_command(ctx, "instagram")

    voice_state = getattr(ctx.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None) if voice_state else None
    if voice_channel is None:
        await safe_respond(
            ctx, "❌ Tenés que estar en un canal de voz para iniciar un stream."
        )
        return

    success, status_msg = await start_instagram_stream_logic(
        ctx.guild_id, voice_channel
    )

    if success:
        if redirect_ch:
            await redirect_ch.send(content=f"<@{ctx.author.id}> {status_msg}")
        else:
            await safe_respond(ctx, status_msg)
    else:
        if redirect_ch:
            await redirect_ch.send(content=f"<@{ctx.author.id}> {status_msg}")
        else:
            await safe_respond(ctx, status_msg)


@bot.slash_command(
    name="sensibilidad",
    description="Cambia la sensibilidad del wake-word del indio (presets 1-4)",
)
async def sensibilidad(
    ctx,
    preset: discord.Option(
        int,
        description="1=máx sensible, 2=solo che indio, 3=pool grande, 4=como 2 + Whisper confirma indio (default)",
        choices=[1, 2, 3, 4],
    ),
):
    """Slash command: switch the VOSK wake-word sensitivity preset.

    Args:
        ctx: Discord application context.
        preset: Integer 1-4 selecting the sensitivity preset.

    Side Effects:
        POSTs to the userbot relay ``/sensibilidad`` which updates the active
        wake-word pattern set and rebuilds the VOSK grammar in-memory.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "sensibilidad")

    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        await safe_respond(ctx, "❌ El relay del indio no está configurado.")
        return

    url = urljoin(config.INDIO_RELAY_URL, "/sensibilidad")
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    payload = {"preset": preset}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    log.warning(
                        "sensibilidad relay HTTP %s: %s", resp.status, body[:200]
                    )
                    await safe_respond(
                        ctx, f"⚠️ No pude cambiar la sensibilidad (HTTP {resp.status})."
                    )
                    return
    except Exception as e:
        log.exception("sensibilidad relay failed")
        await safe_respond(ctx, f"⚠️ Error llamando al indio: {e}")
        return

    _PRESET_DESCRIPTIONS = {
        1: "**Preset 1** — más sensible: `che indio`, `que indio`, `eh indio` + verbos.",
        2: '**Preset 2** — menos sensible: solo `che indio` + verbos. Reduce falsos positivos de "que".',
        3: "**Preset 3** — menos sensible vía pool grande de frases, pero re-habilita `che/que/eh indio`. Editable a mano.",
        4: "**Preset 4** (default) — como el 2 (VOSK: solo `che indio`), pero Whisper re-chequea que se haya dicho `indio` en la región del wake-word; si no, descarta.",
    }
    await safe_respond(
        ctx, f"🎙️ Sensibilidad actualizada → {_PRESET_DESCRIPTIONS[preset]}"
    )


@bot.slash_command(
    name="ranking",
    description="Muestra el ranking MMR de actividad en el servidor",
)
async def ranking(ctx):
    """Slash command: show the MMR leaderboard for this guild."""
    await safe_defer(ctx)
    _track_command(ctx, "ranking")
    if ctx.guild is None:
        await safe_respond(ctx, "❌ Este comando solo funciona en un servidor.")
        return

    data = await _fetch_activity(
        "/activity/leaderboard", {"guild_id": ctx.guild.id, "limit": "15"}
    )
    if data is None:
        await safe_respond(ctx, "❌ No pude obtener el ranking (relay no disponible).")
        return

    rows = data.get("leaderboard", [])
    if not rows:
        await safe_respond(
            ctx, "📊 Todavía no hay datos de actividad en este servidor."
        )
        return

    embed = discord.Embed(
        title="🏆 Ranking MMR",
        description="Top 15 usuarios por MMR en este servidor",
        color=0xE94560,
    )
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows[:15]):
        uid = row["user_id"]
        rating = round(row["rating"], 1)
        prefix = medals[i] if i < 3 else f"**{i + 1}.**"
        member = ctx.guild.get_member(uid)
        if member is None:
            try:
                member = await ctx.guild.fetch_member(uid)
            except Exception:
                member = None
        name = (
            member.display_name
            if member
            else (row.get("display_name") or f"Usuario {uid}")
        )
        embed.add_field(
            name=f"{prefix} {name}",
            value=f"MMR: **{rating}**",
            inline=False,
        )

    try:
        await ctx.followup.send(embed=embed)
    except Exception:
        await safe_respond(ctx, "No pude mostrar el ranking.")


@bot.slash_command(
    name="actividad",
    description="Muestra tus estadísticas de actividad (solo owner)",
)
async def actividad(ctx):
    """Slash command (owner-only): show personal MMR stats.

    Can be used in DMs to the bot. Only the configured OWNER_ID may invoke it.
    """
    await safe_defer(ctx, ephemeral=True)
    _track_command(ctx, "actividad")
    if ctx.author.id != config.OWNER_ID:
        await safe_respond(
            ctx, "❌ Este comando es solo para el owner del bot.", ephemeral=True
        )
        return

    guild = ctx.guild
    if guild is None:
        # Try to pick the first mutual guild for the owner
        for g in bot.guilds:
            if g.get_member(ctx.author.id):
                guild = g
                break
    if guild is None:
        await safe_respond(
            ctx, "❌ No estás en ningún servidor con el bot.", ephemeral=True
        )
        return

    data = await _fetch_activity(
        "/activity/user",
        {"user_id": ctx.author.id, "guild_id": guild.id},
    )
    if data is None:
        await safe_respond(
            ctx,
            "❌ No pude obtener estadísticas (relay no disponible).",
            ephemeral=True,
        )
        return

    stats = data.get("stats")
    if stats is None:
        await safe_respond(
            ctx,
            "📊 Todavía no tenés actividad registrada en este servidor.",
            ephemeral=True,
        )
        return

    rating = round(stats.get("rating", 1500), 1)
    deviation = round(stats.get("deviation", 350), 1)
    total = stats.get("total_activities", 0)
    premium = "Sí" if stats.get("premium") else "No"
    recent = stats.get("recent_activities", [])

    lines = [
        f"📊 **Actividad en {guild.name}**",
        "",
        f"Rating MMR: **{rating}**",
        f"Desviación: {deviation}",
        f"Actividades totales: {total}",
        f"Premium: {premium}",
    ]
    if recent:
        lines.append("")
        lines.append("**Últimos 7 días:**")
        for act in recent[:10]:
            atype = act.get("activity_type", "?")
            cnt = act.get("cnt", 0)
            delta = round(act.get("total_delta", 0), 2)
            sign = "+" if delta >= 0 else ""
            lines.append(f"• {atype}: {cnt}x ({sign}{delta})")

    await safe_respond(ctx, "\n".join(lines), ephemeral=True)


@bot.slash_command(
    name="estadisticas",
    description="Muestra tus estadísticas de voz y ranking MMR",
)
async def estadisticas(
    ctx,
    usuario: discord.Option(
        discord.Member,
        "Usuario a consultar (opcional, default vos)",
        required=False,
    ) = None,
):
    await safe_defer(ctx, ephemeral=usuario is None)
    _track_command(ctx, "estadisticas")
    if ctx.guild is None:
        await safe_respond(ctx, "❌ Este comando solo funciona en un servidor.")
        return

    target = usuario or ctx.author
    uid = target.id

    # Fetch voice summary (last 7 days)
    now = int(time.time())
    week_ago = now - 7 * 86400
    voice_data = await _fetch_activity(
        "/activity/voice-summary",
        {"user_id": uid, "guild_id": ctx.guild.id, "since": str(week_ago)},
    )

    # Fetch leaderboard to calculate ranking position
    lb_data = await _fetch_activity(
        "/activity/leaderboard", {"guild_id": ctx.guild.id, "limit": "999"}
    )

    # Calculate ranking
    ranking_pos = None
    total_users = 0
    target_rating = None
    if lb_data:
        rows = lb_data.get("leaderboard", [])
        total_users = len(rows)
        for i, row in enumerate(rows):
            if row["user_id"] == uid:
                ranking_pos = i + 1
                target_rating = round(row["rating"], 1)
                break

    embed = discord.Embed(
        title=f"📊 Estadísticas de {target.display_name}",
        color=0xE94560,
    )

    # Ranking
    if ranking_pos:
        embed.add_field(
            name="🏆 Ranking MMR",
            value=f"**#{ranking_pos}** de {total_users} usuarios  •  Rating: **{target_rating}**",
            inline=False,
        )
    elif total_users > 0:
        embed.add_field(
            name="🏆 Ranking MMR",
            value=f"Fuera del ranking ({total_users} usuarios activos)",
            inline=False,
        )
    else:
        embed.add_field(
            name="🏆 Ranking MMR",
            value="Todavía no hay datos",
            inline=False,
        )

    # Voice stats
    if voice_data and voice_data.get("summary"):
        s = voice_data["summary"]
        conn_h = s["total_connected"] / 3600
        muted_h = s["total_muted"] / 3600
        embed.add_field(
            name="🎙️ Voz (última semana)",
            value=(
                f"🕐 **{conn_h:.1f}h** en canales de voz\n"
                f"🔇 **{muted_h:.1f}h** muteado\n"
                f"📞 **{s['sessions']}** sesiones"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="🎙️ Voz (última semana)",
            value="Sin actividad de voz registrada",
            inline=False,
        )

    try:
        await ctx.followup.send(embed=embed)
    except Exception:
        await safe_respond(ctx, "No pude mostrar las estadísticas.")


@bot.slash_command(
    name="huh",
    description="Activa/desactiva el sonido de confirmación al detectar wake-word — hecho con ayuda de chipotlai",
)
async def huh(ctx):
    await safe_defer(ctx, ephemeral=True)
    _track_command(ctx, "huh")

    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        await safe_respond(
            ctx, "❌ El relay del indio no está configurado.", ephemeral=True
        )
        return

    url = urljoin(config.INDIO_RELAY_URL, "/toggle_wake_sound")
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json={}, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    await safe_respond(
                        ctx, "⚠️ No pude cambiar el estado del sonido.", ephemeral=True
                    )
                    return
                enabled = body.get("enabled", False)
    except Exception as e:
        log.exception("huh relay failed")
        await safe_respond(ctx, f"⚠️ Error llamando al indio: {e}", ephemeral=True)
        return

    status = "✅ Activado" if enabled else "❌ Desactivado"
    await safe_respond(ctx, f"🎵 Sonido de wake-word: {status}", ephemeral=True)


@bot.slash_command(
    name="transferir",
    description="Subí archivos de hasta 10 GB para compartir en el server",
)
async def transferir(
    ctx,
    dias: discord.Option(
        int,
        "Días que dura el archivo (1-30, default 1)",
        required=False,
        min_value=1,
        max_value=30,
        default=1,
    ) = 1,
):
    """Slash command: generate a temp upload link for file sharing.

    Only users with the configured role (@Main Characters by default) may
    invoke this. The resulting token expires in 5 min unless a file is
    uploaded, and completed files auto-delete after the configured TTL.
    """
    await safe_defer(ctx, ephemeral=True)
    _track_command(ctx, "transferir")

    role = discord.utils.get(ctx.author.roles, name=config.TRANSFER_REQUIRED_ROLE)
    if not role:
        await safe_respond(
            ctx,
            f"❌ Solo @{config.TRANSFER_REQUIRED_ROLE} puede usar este comando.",
            ephemeral=True,
        )
        return

    sess = transferManager.create_session(
        ctx.author.id,
        ctx.author.display_name,
        ctx.channel_id,
        ctx.guild.id,
        days=dias,
    )
    link = f"{config.TRANSFER_BASE_URL}/upload/{sess.token}"
    gb = config.TRANSFER_DEFAULT_LIMIT // (1024**3)
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            label="⬆️ Subir acá",
            url=link,
        )
    )
    dias_str = f"{dias} día{'s' if dias > 1 else ''}"
    try:
        if ctx.response.is_done():
            await ctx.followup.send(
                f"📁 Max {gb} GB · Link vence en {config.TRANSFER_SESSION_TTL // 60} min"
                f" · Archivo disponible {dias_str}",
                view=view,
                ephemeral=True,
            )
        else:
            await ctx.respond(
                f"📁 Max {gb} GB · Link vence en {config.TRANSFER_SESSION_TTL // 60} min"
                f" · Archivo disponible {dias_str}",
                view=view,
                ephemeral=True,
            )
    except Exception:
        pass


@bot.slash_command(
    name="help", description="Lista los comandos del bot y cómo funciona"
)
async def help_cmd(ctx):
    """Slash command: list available commands and bot/userbot info.

    Args:
        ctx: Discord application context.

    Side Effects:
        Sends an ephemeral embed with the command list and contributors.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=True)
    except Exception:
        pass
    _track_command(ctx, "help")

    embed = discord.Embed(
        title="🎙️ VaPls — ayuda",
        description=(
            "Bot de voz/música + persona Gemini con memoria. "
            "Corre en **dos procesos**:\n"
            "• **Main bot** (este) — slash commands, música, soundpad, Gemini.\n"
            "• **Userbot (Indio)** — escucha voz en canales E2EE, transcribe "
            "con faster-whisper y responde al wake-word *indio*."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="🎵 Música y voz",
        value=(
            "**/play** `query` — busca o pega una URL de YouTube y la "
            "reproduce. Con varios resultados muestra menú.\n"
            "**/soundpad** `[query]` — abre el panel de clips locales, o "
            "reproduce el que más se parezca a `query`.\n"
            "**/entraindio** — hace que el indio (userbot) entre a tu canal "
            "de voz para escuchar y responder al wake-word.\n"
            "**/stream** `canal` — transmití un canal de IPTV en tu canal "
            "de voz (Go Live, usa libopenh264).\n"
            "**/stopstream** — detiene la transmisión de IPTV en curso.\n"
            "**/sensibilidad** `1|2|3|4` — ajusta la sensibilidad del wake-word "
            "(1=máxima, 2=solo 'che indio', 3=pool grande+que/eh indio, "
            "4=como 2 + Whisper confirma 'indio' (default)).\n"
            "**/parar** — corta la reproducción, limpia la cola y se "
            "desconecta.\n"
            "**/quit** — sale del canal de voz sin tocar la cola."
        ),
        inline=False,
    )
    embed.add_field(
        name="🤖 Gemini",
        value=(
            "**/vapls** `pregunta` — respuesta puntual, sin memoria.\n"
            "**/indio** `charla` — persona con memoria corta por guild y "
            "memoria larga destilada (rasgos, anécdotas, chistes internos). "
            "También responde por voz cuando lo nombrás en un canal donde "
            "está el userbot.\n"
            "**/generarimagen** `prompt` — genera una imagen con Hugging Face "
            "(gratis, requiere token)."
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Otros",
        value=(
            "**/sugerencias** `idea` — mandá una sugerencia o feature; se "
            "agrupa con ideas parecidas.\n"
            "**/sugerencias-ver** — mirá qué sugerencias ya existen, ordenadas "
            "por las más pedidas.\n"
            "**/transferir** — generá un link para compartir archivos de hasta "
            "10 GB (solo @Main Characters).\n"
            "**/help** — esto."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔑 API keys de Gemini",
        value=(
            "El pool de keys está bancado por la comunidad. Si querés "
            "sumar la tuya, mandámela por **DM al bot** "
            "(formato `AIzaSy…` o `AQ.Ab8RN6…`). Se suma en caliente, sin "
            "reinicio, y queda asociada a tu user para darte crédito."
        ),
        inline=False,
    )
    contributors = geminiKeys.format_contributors_line()
    if contributors:
        embed.set_footer(text=contributors)
    try:
        await ctx.followup.send(embed=embed, ephemeral=True)
    except Exception:
        await safe_respond(ctx, "No pude mandar el help — fijate los logs.")


@bot.slash_command(
    name="spacewar",
    description="Guía para tener Spacewar gratis en tu biblioteca de Steam",
)
async def spacewar(ctx):
    """Slash command: explain how to get Spacewar on Steam.

    Ephemeral guide for users who want to add Steam's free dev-test app
    to their library without purchasing anything.

    Args:
        ctx: Discord application context.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx, ephemeral=True)
    _track_command(ctx, "spacewar")

    await safe_respond(ctx, SPACEWAR_GUIDE_TEXT, ephemeral=True)


async def _render_pet(pet: dict, gif: bool = False) -> discord.File | None:
    if not os.path.isfile(config.PETS_RENDERER):
        return None
    args = ["node", config.PETS_RENDERER]
    if gif:
        args.append("--gif")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        raw = json.dumps(pet)
        stdout, stderr = await asyncio.wait_for(proc.communicate(raw.encode()), timeout=15)
        if proc.returncode != 0:
            log.warning("pet render failed: %s", stderr.decode()[:200])
            return None
        ext = "gif" if gif else "png"
        return discord.File(io.BytesIO(stdout), f"mascota.{ext}")
    except Exception as e:
        log.warning("pet render error: %s", e)
        return None


def _build_pet_msg(pet, formatted, evo_tag, pts=None):
    lines = [f"**{formatted}**{evo_tag}"]
    
    rarity_line = f"*{pet['rarity'].capitalize()}*"
    acc_name = pet.get("parts", {}).get("acc", {}).get("name")
    if acc_name:
        rarity_line += f" — Accesorio: {acc_name.capitalize()}"
    lines.append(rarity_line)
    
    lines.append(f"ATK {pet['stats']['atk']}  DEF {pet['stats']['def']}  MAG {pet['stats']['mag']}  SPD {pet['stats']['spd']}")
    
    if pts is not None:
        lines.append(f"\n⭐ Puntos: {pts['available']:.0f} (usados: {pts['spent'] + pts['reserved']:.0f})")
    return "\n".join(lines)


class InfoView(discord.ui.View):
    def __init__(self, mascota_view):
        super().__init__(timeout=60)
        self.mascota_view = mascota_view

    @discord.ui.button(label="⬅️ Volver", style=discord.ButtonStyle.secondary)
    async def volver(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.mascota_view.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        
        await interaction.response.defer()
        await self.mascota_view._update_mascota_message(
            interaction,
            self.mascota_view.pet,
            self.mascota_view.formatted,
            self.mascota_view.evo_tag,
            "👀 **Volviste a la vista de tu mascota**"
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="⏰ Menú expirado.", view=None, delete_after=1)
        except Exception:
            pass


class MascotaView(discord.ui.View):
    def __init__(self, pet, formatted, evo_tag, pts, channel, uid, ctx):
        super().__init__(timeout=60)
        self.pet = pet
        self.formatted = formatted
        self.evo_tag = evo_tag
        self.pts = pts
        self.channel = channel
        self.uid = uid
        self.ctx = ctx
        self.revertir.disabled = pet.get("evolution_level", 0) == 0

    async def _send_to_channel(self, interaction, gif=False):
        msg = _build_pet_msg(self.pet, self.formatted, self.evo_tag)
        file = await _render_pet(self.pet, gif=gif)
        kwargs = {"content": f"📢 **{interaction.user.display_name}** muestra su mascota:\n{msg}"}
        if file:
            kwargs["file"] = file
        try:
            await self.channel.send(**kwargs)
        except Exception as e:
            log.warning("_send_to_channel failed: %s", e)

    async def _gid(self):
        return self.channel.guild.id if self.channel else 0

    async def _update_mascota_message(self, interaction, pet, formatted, evo_tag, msg):
        self.pet = pet
        self.formatted = formatted
        self.evo_tag = evo_tag
        pts = await _fetch_pet_points(int(self.uid), await self._gid())
        self.pts = pts
        self.revertir.disabled = pet.get("evolution_level", 0) == 0
        full = f"{msg}\n{_build_pet_msg(pet, formatted, evo_tag, pts)}"
        try:
            file = await _render_pet(pet)
            kwargs = {"content": full, "view": self}
            if file:
                kwargs["file"] = file
                kwargs["attachments"] = []
            await interaction.edit_original_response(**kwargs)
        except Exception as e:
            log.warning("_update_mascota_message edit failed: %s", e)

    @discord.ui.button(label="👁 Mostrar", style=discord.ButtonStyle.primary)
    async def mostrar(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        button.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            await self._send_to_channel(interaction, gif=True)
        except Exception as e:
            log.warning("mostrar send failed: %s", e)
        await self._update_mascota_message(interaction, self.pet, self.formatted, self.evo_tag, "✅ Publicado en el canal.")

    @discord.ui.button(label="⬆ Evolucionar", style=discord.ButtonStyle.success)
    async def evolucionar(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        pts = await _fetch_pet_points(int(self.uid), await self._gid())
        if pts["available"] < config.PET_EVOLUTION_COST:
            await interaction.response.send_message(
                f"❌ Necesitás **{config.PET_EVOLUTION_COST}** puntos para evolucionar. Tenés **{pts['available']:.0f}**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        ok = await _post_pet_points("/pet-points/reserve", int(self.uid), await self._gid(), config.PET_EVOLUTION_COST)
        if not ok:
            await interaction.edit_original_response(content="❌ Error al reservar puntos.", view=self)
            return
        new_pet = petGenerator.evolve_pet(self.pet)
        petGenerator.save_pet(self.uid, new_pet)
        evo_tag = f" [+{new_pet.get('evolution_level', 0)}]"
        formatted = petGenerator.format_name(new_pet["name"], new_pet["rarity"])
        log.info("MASCOTA evolucionar uid=%s lvl=%s rarity=%s", self.uid, new_pet.get("evolution_level", 0), new_pet["rarity"])
        await self._update_mascota_message(interaction, new_pet, formatted, evo_tag, "⬆️ **Evolucionó!**")

    @discord.ui.button(label="⬇ Revertir", style=discord.ButtonStyle.secondary)
    async def revertir(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        if self.pet.get("evolution_level", 0) <= 0:
            await interaction.response.send_message("❌ Ya está en su forma base.", ephemeral=True)
            return
        old = petGenerator.revert_pet(self.pet)
        if old is None:
            await interaction.response.send_message("❌ No se puede revertir más.", ephemeral=True)
            return
        await interaction.response.defer()
        await _post_pet_points("/pet-points/release", int(self.uid), await self._gid(), config.PET_EVOLUTION_COST)
        petGenerator.save_pet(self.uid, old)
        evo_tag = f" [+{old.get('evolution_level', 0)}]" if old.get("evolution_level", 0) else ""
        formatted = petGenerator.format_name(old["name"], old["rarity"])
        log.info("MASCOTA revertir uid=%s lvl=%s rarity=%s", self.uid, old.get("evolution_level", 0), old["rarity"])
        await self._update_mascota_message(interaction, old, formatted, evo_tag, "⬇️ **Revertido!**")

    @discord.ui.button(label="📜 Historial", style=discord.ButtonStyle.secondary)
    async def historial(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        original_seed = self.pet.get("original_seed", self.pet["seed"])
        evo_level = self.pet.get("evolution_level", 0)
        chain = petGenerator.rebuild_evolution_chain(original_seed, evo_level)
        lines = ["📜 **Historial evolutivo**\n"]
        for entry in chain:
            lvl = entry["level"]
            marker = "⬅️ **Actual**" if lvl == evo_level else f"`Nvl {lvl}`"
            acc_name = entry.get("parts", {}).get("acc", {}).get("name")
            acc_str = f" (+{acc_name.capitalize()})" if acc_name else ""
            lines.append(
                f"{marker} — {entry['rarity'].capitalize()}{acc_str} "
                f"`{entry['name']}` "
                f"[ATK {entry['stats']['atk']} DEF {entry['stats']['def']} "
                f"MAG {entry['stats']['mag']} SPD {entry['stats']['spd']}]"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="❔ Info", style=discord.ButtonStyle.secondary)
    async def info(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        
        info_text = (
            "ℹ️ **Guía de Mascotas**\n\n"
            "🌟 **Puntos**: Ganás puntos de mascota de forma pasiva por usar Discord.\n"
            "🎀 **Accesorios**: Se generan por semilla. Tienen rareza propia y pueden cambiar por uno mejor o desaparecer temporalmente si el sistema así lo decide en cada nivel.\n"
            "⬆️ **Evolucionar**: Cuesta 300 puntos. Es una evolución conservativa: mantiene el cuerpo original intacto pero escoge una parte al azar (cuerpo, ojos, etc.) para aumentar su rareza al siguiente nivel de forma permanente (y sube los stats).\n"
            "⬇️ **Revertir**: Anula tu última evolución, devolviéndote los 300 puntos para poder intentarlo luego si no te gustó o preferís la forma anterior."
        )
        info_view = InfoView(self)
        await interaction.response.edit_message(content=info_text, view=info_view, attachments=[])

    @discord.ui.button(label="✖ Cerrar", style=discord.ButtonStyle.danger)
    async def cerrar(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != int(self.uid):
            await interaction.response.send_message("❌ No es tu mascota.", ephemeral=True)
            return
        await interaction.response.edit_message(content="✖ Menú cerrado.", view=None, delete_after=1)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="⏰ Menú expirado.", view=None, delete_after=1)
        except Exception:
            pass

    async def on_error(self, error, item, interaction):
        log.error("MascotaView on_error item=%s err=%s", item.custom_id if item else "?", error)


@bot.slash_command(
    name="mascota",
    description="Gestiona tu Mascota - ver, mostrar",
)
async def mascota(
    ctx,
    accion: discord.Option(
        str,
        "ver (default) | mostrar",
        choices=["ver", "mostrar"],
        default="ver",
    ) = "ver",
):
    _track_command(ctx, "mascota", {"accion": accion})
    uid = str(ctx.author.id)
    guild_id = ctx.guild.id if ctx.guild else 0
    channel = ctx.channel if ctx.guild else None

    if accion == "mostrar":
        pet = petGenerator.get_pet(uid)
        if pet is None:
            await ctx.respond( "❌ No tenés mascota. Usá `/mascota` para crear una.", ephemeral=True)
            return
        formatted = petGenerator.format_name(pet["name"], pet["rarity"])
        evo = pet.get("evolution_level", 0)
        evo_tag = f" [+{evo}]" if evo else ""
        msg = _build_pet_msg(pet, formatted, evo_tag)
        file = await _render_pet(pet, gif=True)
        kwargs = {"content": f"📢 **{ctx.author.display_name}** muestra su mascota:\n{msg}"}
        if file:
            kwargs["file"] = file
        await channel.send(**kwargs)
        await ctx.respond("✅ Mascota publicada en el canal.", ephemeral=True)
        return

    pet = petGenerator.get_or_create_pet(uid)
    pts = await _fetch_pet_points(ctx.author.id, guild_id)
    formatted = petGenerator.format_name(pet["name"], pet["rarity"])
    evo = pet.get("evolution_level", 0)
    evo_tag = f" [+{evo}]" if evo else ""
    view = MascotaView(pet, formatted, evo_tag, pts, channel, uid, ctx)
    msg = _build_pet_msg(pet, formatted, evo_tag, pts)
    log.info("MASCOTA ver uid=%s rarity=%s evo=%s pts=%.0f", uid, pet["rarity"], evo, pts["available"])
    
    file = await _render_pet(pet)
    kwargs = {"content": msg, "ephemeral": True, "view": view}
    if file:
        kwargs["file"] = file
    r = await ctx.respond(**kwargs)
    view.message = r


@bot.slash_command(
    name="story-test",
    description="[owner] Forzar una historia del Indio ahora (testing)",
)
async def story_test(ctx):
    """Slash command (owner-only): trigger an Indio story immediately.

    Useful for testing the full pipeline: pick image → Gemini → post review.
    The story is posted to the review channel and counts toward the daily max.
    """
    await safe_defer(ctx, ephemeral=True)
    _track_command(ctx, "story-test")
    if ctx.author.id != config.OWNER_ID:
        await safe_respond(ctx, "❌ Solo el owner puede usar esto.", ephemeral=True)
        return
    if ctx.guild is None:
        await safe_respond(ctx, "❌ Solo funciona en un servidor.", ephemeral=True)
        return

    guild_id = ctx.guild.id
    channel_id = config.INDIO_STORY_CHANNEL_ID
    try:
        ok = await storyManager.trigger_story(
            bot, guild_id, channel_id, trigger_type="test"
        )
        if ok:
            await safe_respond(
                ctx,
                "🐔 Historia disparada — revisá <#{}>.".format(channel_id),
                ephemeral=True,
            )
        else:
            await safe_respond(
                ctx,
                "❌ No se pudo disparar la historia. Revisá los logs.",
                ephemeral=True,
            )
    except Exception as e:
        log.exception("story-test failed")
        await safe_respond(ctx, f"❌ Error: {e}", ephemeral=True)


@bot.slash_command(
    name="alert-test",
    description="[owner] Send a test alert to verify the alert system",
)
async def alert_test(ctx):
    """Slash command (owner-only): post a test alert embed."""
    await safe_defer(ctx, ephemeral=True)
    _track_command(ctx, "alert-test")
    if ctx.author.id != config.OWNER_ID:
        await safe_respond(ctx, "❌ Solo el owner puede usar esto.", ephemeral=True)
        return

    if not config.ISRAEL_ALERTS_ENABLED or not config.ISRAEL_ALERTS_CHANNEL_ID:
        await safe_respond(
            ctx,
            "❌ Israel alerts no está configurado (ISRAEL_ALERTS_ENABLED o ISRAEL_ALERTS_CHANNEL_ID).",
            ephemeral=True,
        )
        return

    try:
        from israel_alerts import IsraelAlertListener, _THREAT_MAP

        ch = bot.get_channel(config.ISRAEL_ALERTS_CHANNEL_ID)
        if ch is None:
            ch = await bot.fetch_channel(config.ISRAEL_ALERTS_CHANNEL_ID)

        # Build a sample embed as if it were a real alert
        listener = IsraelAlertListener(bot, config.ISRAEL_ALERTS_CHANNEL_ID)
        # Inject a few known cities to test the mapping
        listener._city_map = {"תל אביב - מרכז העיר": "Tel Aviv - City Center"}
        listener._zone_map = {"תל אביב - מרכז העיר": "Dan"}

        embed = listener._build_embed(
            _THREAT_MAP[0],
            ["Tel Aviv - City Center"],
            ("Unknown", "Unknown", 0),
            {"time": int(time.time())},
        )
        await ch.send(embed=embed)
        await safe_respond(
            ctx,
            "✅ Test alert sent to <#{}>.".format(config.ISRAEL_ALERTS_CHANNEL_ID),
            ephemeral=True,
        )
    except Exception as e:
        log.exception("alert-test failed")
        await safe_respond(ctx, f"❌ Error: {e}", ephemeral=True)


@bot.slash_command(name="restart", description="devtool - reinicia procesos del bot (vapls/indio/golive)")
async def restart(
    ctx,
    target: discord.Option(
        str,
        description="Qué reiniciar (default: todo)",
        choices=["vapls", "indio", "golive", "all"],
        required=False,
        default="all",
    ) = "all",
):
    """Slash command: restart bot processes (dev-only).

    Args:
        ctx: Discord application context.
        target: Which process(es) to restart: vapls, indio, golive, or all.

    Side Effects:
        Calls os.execv to replace the current vapls process, and/or
        sudo systemctl restart for indio/golive userbots.

    Async:
        This function is a coroutine and must be awaited.
    """
    _track_command(ctx, "restart", {"target": target})
    await ctx.defer(ephemeral=True)

    async def _svc(name: str) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return stderr.decode(errors="replace").strip()
        return None

    parts: list[str] = []
    errs: list[str] = []

    if target in ("indio", "all"):
        err = await _svc("indio-userbot")
        if err:
            errs.append(f"indio: {err}")
        else:
            parts.append("indio")

    if target in ("golive", "all"):
        err = await _svc("golive-userbot")
        if err:
            errs.append(f"golive: {err}")
        else:
            parts.append("golive")

    msg = ""
    if parts:
        msg += "♻️ " + " ".join(p.capitalize() for p in parts) + " reiniciado."
    if errs:
        msg += "\n❌ " + "\n".join(errs)
    if target in ("vapls", "all"):
        msg += "\n♻️ Reiniciando Vapls..."
    await ctx.followup.send(msg or "✅ Sin cambios.", ephemeral=True)

    log.info("[RESTART] target=%s parts=%s", target, parts)

    if target in ("vapls", "all"):
        analytics.shutdown()
        os.execv(
            sys.executable,
            [sys.executable, "/home/ubuntu/vapls-discord-bot/bot.py"],
        )


if __name__ == "__main__":
    try:
        bot.run(config.TOKEN)
    finally:
        analytics.shutdown()
        # try:
        #     import asyncio
        #
        #     asyncio.run(geminiImage.close())
        # except Exception:
        #     pass
