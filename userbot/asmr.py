"""ASMR ambient playback module for the userbot (Indio).

Plays a random audio file from the ``asmr`` directory at a randomized lower
volume (50% to 90%) at random moments under strict conditions:
- At most once per day (24h cooldown + calendar day check, persisted to disk).
- Only when a human user is connected in the voice channel for at least 30 minutes (1800s).
- Neither the userbot nor the human user is muted or deafened.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import time
from typing import Optional

import discord

import config

logger = logging.getLogger("userbot.asmr")

_last_played_ts: float = 0.0
_state_loaded: bool = False

# Track member connection timestamps: {(channel_id, member_id): joined_ts}
_member_connected_since: dict[tuple[int, int], float] = {}


def _get_state_file_path() -> str:
    return getattr(config, "ASMR_STATE_PATH", "data/asmr_state.json")


def load_asmr_state(path: Optional[str] = None) -> float:
    """Load the last played timestamp from JSON file into memory."""
    global _last_played_ts, _state_loaded
    file_path = path or _get_state_file_path()
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _last_played_ts = float(raw.get("last_played_ts", 0.0))
        else:
            _last_played_ts = 0.0
    except Exception:
        logger.exception("[ASMR] failed to load state from %s", file_path)
        _last_played_ts = 0.0
    _state_loaded = True
    return _last_played_ts


def save_asmr_state(ts: float, path: Optional[str] = None) -> None:
    """Safely persist ``_last_played_ts`` to JSON file using atomic write."""
    global _last_played_ts
    _last_played_ts = ts
    file_path = path or _get_state_file_path()
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"last_played_ts": ts}, f, indent=2)
        os.replace(tmp_path, file_path)
    except Exception:
        logger.exception("[ASMR] failed to save state to %s", file_path)


def _ensure_state_loaded() -> None:
    global _state_loaded
    if not _state_loaded:
        load_asmr_state()


def can_play_today(now: Optional[float] = None) -> bool:
    """Return True if ASMR has not been played in the last 24 hours and not today."""
    _ensure_state_loaded()
    if _last_played_ts <= 0:
        return True
    if now is None:
        now = time.time()
    if now - _last_played_ts < 86400:
        return False
    dt_now = datetime.datetime.fromtimestamp(now)
    dt_last = datetime.datetime.fromtimestamp(_last_played_ts)
    if dt_now.date() == dt_last.date():
        return False
    return True


def on_voice_state_update(member: discord.Member, before, after) -> None:
    """Update member connection timestamps on voice state changes."""
    now = time.time()
    if before.channel and (not after.channel or after.channel.id != before.channel.id):
        _member_connected_since.pop((before.channel.id, member.id), None)
    if after.channel and (not before.channel or before.channel.id != after.channel.id):
        if not getattr(member, "bot", False) and member.id not in getattr(config, "IGNORE_USER_IDS", set()):
            _member_connected_since[(after.channel.id, member.id)] = now


def resolve_asmr_audio_path() -> Optional[str]:
    """Scans for valid ASMR audio files in candidate directories and picks one at random."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    custom_audio_dir = getattr(config, "CUSTOM_AUDIO_PATH", "/home/ubuntu/vapls-discord-bot/audio_output")
    subdir = getattr(config, "ASMR_SUBDIR", "asmr")

    candidate_dirs = [
        os.path.join(custom_audio_dir, subdir),
        os.path.join(repo_root, "audio_output", subdir),
        os.path.join(repo_root, subdir),
    ]

    valid_exts = {".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac", ".opus"}
    found_files: list[str] = []

    for cdir in candidate_dirs:
        if os.path.isdir(cdir):
            try:
                for root, _, files in os.walk(cdir):
                    for fname in files:
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in valid_exts:
                            found_files.append(os.path.join(root, fname))
            except Exception:
                logger.exception("[ASMR] error reading directory %s", cdir)

    if not found_files:
        return None
    return random.choice(found_files)


def is_member_unmuted(member: discord.Member) -> bool:
    """Return True if the member has an active voice state and is neither muted nor deafened."""
    voice = getattr(member, "voice", None)
    if voice is None:
        return False
    if getattr(voice, "self_mute", False) or getattr(voice, "mute", False):
        return False
    if getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False):
        return False
    return True


def is_bot_unmuted(guild: discord.Guild) -> bool:
    """Return True if the bot's own member in the guild is neither muted nor deafened."""
    me = getattr(guild, "me", None)
    if me is None:
        return True
    voice = getattr(me, "voice", None)
    if voice is None:
        return True
    if getattr(voice, "self_mute", False) or getattr(voice, "mute", False):
        return False
    if getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False):
        return False
    return True


def get_eligible_member(client, vc, now: Optional[float] = None) -> Optional[discord.Member]:
    """Check if ``vc`` and any connected member fulfill all criteria for playing ASMR.

    Returns the eligible ``discord.Member``, or ``None`` if ineligible.
    """
    if not getattr(config, "ASMR_ENABLED", True):
        return None
    if now is None:
        now = time.time()
    if not can_play_today(now):
        return None
    if vc is None or not vc.is_connected() or vc.is_playing():
        return None
    guild = getattr(vc, "guild", None)
    if guild and not is_bot_unmuted(guild):
        return None
    channel = getattr(vc, "channel", None)
    if channel is None:
        return None

    self_id = getattr(getattr(client, "user", None), "id", None)
    min_connected = float(getattr(config, "ASMR_MIN_CONNECTED_SECONDS", 1800.0))
    ignore_ids = getattr(config, "IGNORE_USER_IDS", set())

    for member in getattr(channel, "members", []):
        if getattr(member, "bot", False):
            continue
        if self_id is not None and member.id == self_id:
            continue
        if member.id in ignore_ids:
            continue
        if not is_member_unmuted(member):
            continue

        joined_at = _member_connected_since.get((channel.id, member.id))
        if joined_at is None:
            # If untracked member (e.g. joined before bot started/tracked), initialize join ts
            joined_at = now
            _member_connected_since[(channel.id, member.id)] = joined_at

        if now - joined_at >= min_connected:
            return member

    return None


async def play_asmr(vc) -> bool:
    """Play a random ASMR audio clip on ``vc`` at a volume between 50% and 90%."""
    path = resolve_asmr_audio_path()
    if path is None:
        logger.warning("[ASMR] file missing or no audio files in asmr folder")
        return False

    min_vol = float(getattr(config, "ASMR_MIN_VOLUME", 0.50))
    max_vol = float(getattr(config, "ASMR_MAX_VOLUME", 0.90))
    vol = random.uniform(min_vol, max_vol)

    options = f'-af "volume={vol:.2f}"'
    try:
        try:
            source = discord.FFmpegOpusAudio(path, options=options)
        except Exception:
            source = discord.FFmpegOpusAudio(path)
        vc.play(source)
        now = time.time()
        save_asmr_state(now)
        logger.info("[ASMR] playing %s (volume=%.2f, channel=%s)", path, vol, getattr(vc, "channel", None))
        return True
    except Exception:
        logger.exception("[ASMR] play failed")
        return False


async def asmr_loop(client, interval_seconds: float = 60.0) -> None:
    """Background task polling connected voice channels to trigger ASMR at random moments."""
    while not client.is_closed():
        try:
            await asyncio.sleep(interval_seconds)
            if not getattr(config, "ASMR_ENABLED", True):
                continue
            now = time.time()
            if not can_play_today(now):
                continue

            chance = float(getattr(config, "ASMR_CHANCE_PER_MINUTE", 0.05))
            voice_clients = getattr(client, "voice_clients", ()) or ()
            for vc in voice_clients:
                eligible_member = get_eligible_member(client, vc, now)
                if eligible_member is not None:
                    if random.random() < chance:
                        played = await play_asmr(vc)
                        if played:
                            break
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[ASMR] error in loop")
