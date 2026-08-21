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
import json
import logging
import os
import random
import time
from typing import Optional

import discord

import config

logger = logging.getLogger("userbot.greeting")

FFMPEG_NORMALIZE_OPTS = '-af "dynaudnorm=p=0.95:f=200"'

_last_greeting: dict[int, float] = {}
_last_wake_sound: dict[int, float] = {}

# In-memory pity state: {user_id: {rel_path: miss_count}}
_pity_state: dict[int, dict[str, int]] = {}
_pity_loaded = False


def _get_pity_file_path() -> str:
    return getattr(config, "GREETING_PITY_PATH", "data/greeting_pity.json")


def load_pity_state(path: Optional[str] = None) -> dict[int, dict[str, int]]:
    """Load pity counters from JSON file into in-memory ``_pity_state``."""
    global _pity_state, _pity_loaded
    file_path = path or _get_pity_file_path()
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _pity_state = {
                int(uid): {str(p): int(cnt) for p, cnt in paths.items()}
                for uid, paths in raw.items()
                if isinstance(paths, dict)
            }
        else:
            _pity_state = {}
    except Exception:
        logger.exception("[GREETING] failed to load pity state from %s", file_path)
        _pity_state = {}
    _pity_loaded = True
    return _pity_state


def save_pity_state(path: Optional[str] = None) -> None:
    """Safely persist in-memory ``_pity_state`` to JSON file using atomic write."""
    file_path = path or _get_pity_file_path()
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({str(uid): counts for uid, counts in _pity_state.items()}, f, indent=2)
        os.replace(tmp_path, file_path)
    except Exception:
        logger.exception("[GREETING] failed to save pity state to %s", file_path)


def _ensure_pity_loaded() -> None:
    global _pity_loaded
    if not _pity_loaded:
        load_pity_state()


def calculate_effective_weights(
    items: list,
    user_id: int,
    pity_state: Optional[dict[str, int]] = None,
    rare_threshold: Optional[float] = None,
    member_count: int = 1,
) -> tuple[list[str], list[float], set[str]]:
    """Calculate effective weights for a list of greeting items taking pity and
    channel member count into account.

    Returns:
        (paths, effective_weights, rare_paths)
    """
    if rare_threshold is None:
        rare_threshold = getattr(config, "GREETING_RARE_THRESHOLD", 0.05)

    paths: list[str] = []
    base_weights: list[float] = []
    for item in items:
        if isinstance(item, dict) and "path" in item:
            paths.append(item["path"])
            base_weights.append(float(item.get("weight", 1)))
        elif isinstance(item, str):
            paths.append(item)
            base_weights.append(1.0)

    if not paths:
        return [], [], set()

    total_base = sum(base_weights)
    if total_base <= 0:
        total_base = float(len(base_weights))
        base_weights = [1.0] * len(base_weights)

    user_pity = pity_state if pity_state is not None else (_pity_state.get(user_id) or {})
    rare_paths = set()
    effective_weights: list[float] = []

    people_mult = 1.0 + (max(1, member_count) - 1) / 9.0

    for path, base_w in zip(paths, base_weights):
        base_prob = base_w / total_base
        if base_prob <= rare_threshold:
            rare_paths.add(path)
            misses = max(0, user_pity.get(path, 0))
            # Progressive weight: base_w * (1 + misses) * people_mult
            effective_weights.append(base_w * (1.0 + misses) * people_mult)
        else:
            effective_weights.append(base_w)

    return paths, effective_weights, rare_paths


def _users_map() -> dict:
    """Late import so tests can monkeypatch ``users.USERS`` after import."""
    try:
        from users import USERS
    except Exception:
        return {}
    return USERS or {}


def resolve_greeting_path(
    user_id: int,
    *,
    record_pity: bool = True,
    member_count: int = 1,
) -> Optional[str]:
    """Return the absolute greeting path for a user, or ``None`` when the user
    has no explicit greeting configured.

    Supports three greeting formats:
    - Plain string: ``"Audios/bokita.mp3"``
    - List of strings: ``["a.mp3", "b.mp3"]`` — picks one at random
    - List of dicts with weights: ``[{"path": "a.mp3", "weight": 99}, ...]``

    For weighted items, audios with low base probability (<= 5% by default)
    gain pity / progressive chance on every miss until played, scaled by
    the number of members in the voice channel.

    No default fallback — only users with an explicit ``greeting`` key in
    ``users.USERS`` produce a path.
    """
    if user_id is None:
        return None
    info = _users_map().get(user_id) or {}
    rel = info.get("greeting")
    if not rel:
        return None
    if isinstance(rel, list) and rel:
        _ensure_pity_loaded()
        paths, weights, rare_paths = calculate_effective_weights(
            rel, user_id, member_count=member_count
        )
        if not paths:
            return None
        chosen_path = random.choices(paths, weights=weights, k=1)[0]
        if record_pity and rare_paths:
            user_counts = _pity_state.setdefault(user_id, {})
            if chosen_path in rare_paths:
                user_counts[chosen_path] = 0
            for r_path in rare_paths:
                if r_path != chosen_path:
                    user_counts[r_path] = user_counts.get(r_path, 0) + 1
            save_pity_state()
        rel = chosen_path
    if not isinstance(rel, str):
        return None
    primary = os.path.join(config.CUSTOM_AUDIO_PATH, rel)
    if os.path.exists(primary):
        return primary
    if rel.startswith("Audios/") or rel.startswith("Audios\\"):
        alt = os.path.join(config.CUSTOM_AUDIO_PATH, rel[7:])
        if os.path.exists(alt):
            return alt
    return primary


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
    info = _users_map().get(user_id) or {}
    if not info.get("greeting"):
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
    member_count = 1
    try:
        channel = getattr(vc, "channel", None)
        if channel and hasattr(channel, "members"):
            humans = [m for m in channel.members if not getattr(m, "bot", False)]
            member_count = len(humans) if humans else len(channel.members)
    except Exception:
        member_count = 1
    path = resolve_greeting_path(user_id, member_count=member_count)
    if path is None:
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


def resolve_wake_sound_path() -> Optional[str]:
    """Return the absolute wake-sound path, or ``None`` when unconfigured."""
    rel = getattr(config, "WAKE_SOUND_PATH", "") or ""
    if not rel:
        return None
    if os.path.isabs(rel):
        return rel
    return os.path.join(config.CUSTOM_AUDIO_PATH, rel)


def _find_vc_with_user(client, user_id: int):
    """Return the first connected voice client whose channel contains ``user_id``."""
    for vc in getattr(client, "voice_clients", ()) or ():
        try:
            channel = getattr(vc, "channel", None)
            if channel is None:
                continue
            if any(getattr(m, "id", None) == user_id for m in channel.members):
                return vc
        except Exception:
            continue
    return None


async def play_wake_sound(client, *, user_id: int) -> bool:
    """Play the configured wake sound on the VC where ``user_id`` is currently
    sitting. Returns ``True`` when audio was scheduled, ``False`` when skipped
    (feature disabled, no path configured, user not in a connected VC, vc busy,
    file missing, or throttled). Errors are logged and swallowed.
    """
    if not getattr(config, "WAKE_SOUND_ENABLED", True):
        return False
    path = resolve_wake_sound_path()
    if path is None:
        return False
    vc = _find_vc_with_user(client, user_id)
    if vc is None:
        return False
    try:
        if not vc.is_connected():
            return False
    except Exception:
        return False
    channel_id = getattr(getattr(vc, "channel", None), "id", None)
    if channel_id is None:
        return False
    now = time.time()
    last = _last_wake_sound.get(channel_id, 0.0)
    if now - last < config.WAKE_SOUND_THROTTLE_SECONDS:
        logger.info(
            "[WAKE-SOUND] throttled (channel=%s, %.1fs since last)",
            channel_id, now - last,
        )
        return False
    try:
        if vc.is_playing():
            logger.info("[WAKE-SOUND] vc already playing (channel=%s)", channel_id)
            return False
    except Exception:
        return False
    if not os.path.exists(path):
        logger.warning("[WAKE-SOUND] file missing: %s", path)
        return False
    _last_wake_sound[channel_id] = now
    try:
        source = discord.FFmpegOpusAudio(path, options=FFMPEG_NORMALIZE_OPTS)
        vc.play(source)
        logger.info("[WAKE-SOUND] playing %s (user=%s, channel=%s)",
                    path, user_id, channel_id)
        return True
    except Exception:
        logger.exception("[WAKE-SOUND] play failed (channel=%s)", channel_id)
        return False
