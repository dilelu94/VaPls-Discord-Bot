"""Per-user greeting playback for the userbot.

Reuses ``users.USERS`` from the main bot (the userbot already imports it for
naming). A greeting fires when a human joins the voice channel the userbot is
sitting in, but ONLY for users that have an explicit ``greeting`` audio path
in ``users.USERS`` — there is no default fallback. Users without a configured
greeting trigger nothing.

Throttled per-channel (default 15s) so a flurry of joins doesn't queue up a
chain of audio. Loudness is normalized with ``dynaudnorm`` so quieter clips
come out at the same perceived level as the louder ones.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import discord

import config

logger = logging.getLogger("userbot.greeting")

FFMPEG_NORMALIZE_OPTS = '-af "dynaudnorm=p=0.95:f=200"'

_last_greeting: dict[int, float] = {}


def _users_map() -> dict:
    """Late import so tests can monkeypatch ``users.USERS`` after import."""
    try:
        from users import USERS
    except Exception:
        return {}
    return USERS or {}


def resolve_greeting_path(user_id: int) -> Optional[str]:
    """Return the absolute greeting path for a user, or ``None`` when the user
    has no explicit greeting configured.

    Unlike the main bot's :func:`greeting._resolve_greeting_path`, there is no
    default fallback: only users with an explicit ``greeting`` key in
    ``users.USERS`` produce a path.
    """
    if user_id is None:
        return None
    info = _users_map().get(user_id) or {}
    rel = info.get("greeting")
    if not rel:
        return None
    return os.path.join(config.CUSTOM_AUDIO_PATH, rel)


async def _wait_until_ready(vc, *, timeout_seconds: float = 10.0) -> bool:
    """Poll ``vc.is_connected()`` for up to ``timeout_seconds``."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if vc is None:
            return False
        try:
            if vc.is_connected():
                return True
        except Exception:
            return False
        await asyncio.sleep(0.25)
    return False


async def play_user_greeting(vc, *, user_id: int, channel_id: int) -> bool:
    """Play the per-user greeting on ``vc`` if eligible.

    Returns ``True`` when audio was scheduled, ``False`` when skipped (no
    configured greeting for this user, throttled, vc not ready, file missing,
    or the feature is disabled). Errors are logged and swallowed.
    """
    if not getattr(config, "GREETING_ENABLED", True):
        return False
    path = resolve_greeting_path(user_id)
    if path is None:
        return False
    now = time.time()
    last = _last_greeting.get(channel_id, 0.0)
    if now - last < config.GREETING_THROTTLE_SECONDS:
        logger.info(
            "[GREETING] throttled (channel=%s, %.1fs since last)",
            channel_id, now - last,
        )
        return False
    if not await _wait_until_ready(vc):
        logger.info("[GREETING] vc never ready (channel=%s)", channel_id)
        return False
    try:
        if vc.is_playing():
            logger.info("[GREETING] vc already playing (channel=%s)", channel_id)
            return False
    except Exception:
        return False
    if not os.path.exists(path):
        logger.warning("[GREETING] file missing: %s", path)
        return False
    _last_greeting[channel_id] = now
    try:
        source = discord.FFmpegOpusAudio(path, options=FFMPEG_NORMALIZE_OPTS)
        vc.play(source)
        logger.info("[GREETING] playing %s (user=%s, channel=%s)",
                    path, user_id, channel_id)
        return True
    except Exception:
        logger.exception("[GREETING] play failed (channel=%s)", channel_id)
        return False
