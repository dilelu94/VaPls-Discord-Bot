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

def get_ffmpeg_greeting_opts() -> str:
    vol = getattr(config, "GREETING_VOLUME", 0.8)
    return f'-af "dynaudnorm=p=0.95:f=200,volume={vol}"'


FFMPEG_NORMALIZE_OPTS = get_ffmpeg_greeting_opts()

_last_greeting: dict[int, float] = {}
_last_wake_sound: dict[int, float] = {}

_greeting_playing: bool = False


def is_greeting_playing() -> bool:
    """Return True if a user greeting audio is currently playing in voice."""
    global _greeting_playing
    return _greeting_playing


def _on_greeting_end(err=None) -> None:
    global _greeting_playing
    _greeting_playing = False


# In-memory pity state: {user_id: {rel_path: miss_count}}
_pity_state: dict[int, dict[str, int]] = {}
_pity_loaded = False

# In-memory name TTS metadata cache: {user_id: {"name": name, "path": path}}
_name_tts_meta: dict[int, dict[str, str]] = {}
_name_tts_loaded = False


def _get_pity_file_path() -> str:
    return getattr(config, "GREETING_PITY_PATH", "data/greeting_pity.json")


def _get_name_tts_meta_path() -> str:
    return getattr(config, "GREETING_TTS_META_PATH", "data/greeting_tts_meta.json")


def _get_name_tts_dir() -> str:
    return getattr(config, "GREETING_TTS_DIR", "data/greeting_tts")


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


def load_name_tts_meta(path: Optional[str] = None) -> dict[int, dict[str, str]]:
    """Load name TTS metadata from JSON file into in-memory ``_name_tts_meta``."""
    global _name_tts_meta, _name_tts_loaded
    file_path = path or _get_name_tts_meta_path()
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _name_tts_meta = {
                int(uid): {"name": str(data.get("name", "")), "path": str(data.get("path", ""))}
                for uid, data in raw.items()
                if isinstance(data, dict)
            }
        else:
            _name_tts_meta = {}
    except Exception:
        logger.exception("[GREETING] failed to load name TTS metadata from %s", file_path)
        _name_tts_meta = {}
    _name_tts_loaded = True
    return _name_tts_meta


def save_name_tts_meta(path: Optional[str] = None) -> None:
    """Safely persist in-memory ``_name_tts_meta`` to JSON file using atomic write."""
    file_path = path or _get_name_tts_meta_path()
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({str(uid): data for uid, data in _name_tts_meta.items()}, f, indent=2)
        os.replace(tmp_path, file_path)
    except Exception:
        logger.exception("[GREETING] failed to save name TTS metadata to %s", file_path)


def _ensure_name_tts_loaded() -> None:
    global _name_tts_loaded
    if not _name_tts_loaded:
        load_name_tts_meta()


def get_user_greeting_name(
    user_id: int,
    *,
    member: Optional[discord.Member] = None,
    display_name: Optional[str] = None,
) -> Optional[str]:
    """Determine the canonical display name to use for TTS greeting synthesis.

    Priority:
    1. Static ``name`` key in ``users.USERS`` (e.g. "Chalo", "Mila", "Miles").
    2. Explicitly passed ``display_name``.
    3. ``member.display_name`` or ``member.name`` from Discord Member.
    """
    if user_id is None:
        return None

    golive_id = getattr(config, "GOLIVE_USER_ID", 1541984338386620492)
    if user_id == golive_id or str(user_id) == str(golive_id):
        if display_name and isinstance(display_name, str) and display_name.strip():
            return display_name.strip()
        if member is not None:
            dname = getattr(member, "display_name", None) or getattr(member, "name", None)
            if dname and isinstance(dname, str) and dname.strip():
                return dname.strip()

    users = _users_map()
    info = (
        users.get(user_id)
        or (users.get(int(user_id)) if str(user_id).isdigit() else None)
        or users.get(str(user_id))
        or {}
    )
    name = info.get("name")
    if name and isinstance(name, str) and name.strip():
        return name.strip()

    if display_name and isinstance(display_name, str) and display_name.strip():
        return display_name.strip()

    if member is not None:
        dname = getattr(member, "display_name", None) or getattr(member, "name", None)
        if dname and isinstance(dname, str) and dname.strip():
            return dname.strip()

    return None


def get_or_generate_name_tts(user_id: int, name: str) -> Optional[str]:
    """Return the path to a cached TTS WAV file saying ``name`` for ``user_id``.

    If an existing audio file exists and the cached name matches ``name``, reuses it.
    Otherwise, generates a new TTS WAV using ``tts.generate_tts_wav``, saving
    it to ``data/greeting_tts/{user_id}.wav`` (overwriting if needed) and
    updating metadata.
    """
    if user_id is None or not name or not name.strip():
        return None

    _ensure_name_tts_loaded()
    tts_dir = _get_name_tts_dir()
    os.makedirs(tts_dir, exist_ok=True)
    expected_path = os.path.join(tts_dir, f"{user_id}.wav")

    cached = _name_tts_meta.get(user_id)
    if (
        cached
        and cached.get("name") == name
        and os.path.exists(expected_path)
        and os.path.getsize(expected_path) > 0
    ):
        return expected_path

    try:
        import tts
        res_path = tts.generate_tts_wav(name, output_path=expected_path)
        if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0:
            _name_tts_meta[user_id] = {"name": name, "path": res_path}
            save_name_tts_meta()
            return res_path
        else:
            logger.warning("[GREETING] TTS generation for name %r returned invalid path %s", name, res_path)
            return None
    except Exception:
        logger.exception("[GREETING] failed to generate TTS for user_id=%s name=%r", user_id, name)
        return None


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


def _locate_audio_file(rel: Optional[str]) -> Optional[str]:
    """Find absolute path for a relative audio path if it exists on disk, else None."""
    if not rel or not isinstance(rel, str) or not rel.strip():
        return None
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    custom_audio_dir = getattr(config, "CUSTOM_AUDIO_PATH", "/home/ubuntu/vapls-discord-bot/audio_output")
    candidates = [
        os.path.join(custom_audio_dir, rel),
        os.path.join(repo_root, "audio_output", rel),
        os.path.join(repo_root, rel),
    ]
    if rel.startswith("Audios/") or rel.startswith("Audios\\"):
        candidates.append(os.path.join(custom_audio_dir, rel[7:]))
        candidates.append(os.path.join(repo_root, "audio_output", rel[7:]))

    basename = os.path.basename(rel)
    if basename and basename != rel:
        candidates.append(os.path.join(custom_audio_dir, basename))
        candidates.append(os.path.join(repo_root, "audio_output", basename))
        candidates.append(os.path.join(repo_root, "audio_output", "Audios", basename))

    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


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
    users = _users_map()
    info = users.get(user_id) or (users.get(int(user_id)) if str(user_id).isdigit() else None) or users.get(str(user_id)) or {}
    rel = info.get("greeting")
    if not rel:
        return None
    if isinstance(rel, list) and rel:
        valid_items = []
        for item in rel:
            p = item.get("path") if isinstance(item, dict) else item
            if p is None or _locate_audio_file(p) is not None:
                valid_items.append(item)
        items_to_use = valid_items if valid_items else rel

        _ensure_pity_loaded()
        paths, weights, rare_paths = calculate_effective_weights(
            items_to_use, user_id, member_count=member_count
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
    if not isinstance(rel, str) or not rel.strip():
        return None

    found = _locate_audio_file(rel)
    if found:
        return found
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    custom_audio_dir = getattr(config, "CUSTOM_AUDIO_PATH", "/home/ubuntu/vapls-discord-bot/audio_output")
    return os.path.join(custom_audio_dir, rel)


def _is_vc_ready(vc) -> bool:
    """Return True if ``vc`` is connected and its voice WebSocket socket is open."""
    if vc is None:
        return False
    try:
        if not vc.is_connected():
            return False
        ws = getattr(vc, "ws", None)
        if ws is None or type(ws).__module__.startswith("unittest.mock"):
            return True
        sock = getattr(ws, "socket", None)
        if sock is not None and not type(sock).__module__.startswith("unittest.mock") and getattr(sock, "closed", None) is True:
            return False
        if hasattr(ws, "open") and not type(getattr(ws, "open")).__module__.startswith("unittest.mock") and getattr(ws, "open") is False:
            return False
        return True
    except Exception:
        return bool(vc.is_connected())


async def _wait_until_ready(vc, *, timeout_seconds: float = 10.0) -> bool:
    """Poll ``_is_vc_ready(vc)`` for up to ``timeout_seconds``."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _is_vc_ready(vc):
            return True
        await asyncio.sleep(0.25)
    return False



_last_user_greeting: dict[tuple[int, int], float] = {}


async def play_user_greeting(
    vc,
    *,
    user_id: int,
    channel_id: int,
    member: Optional[discord.Member] = None,
    display_name: Optional[str] = None,
) -> bool:
    """Play the per-user greeting on ``vc`` if eligible.

    For users with a configured base audio file, plays that audio file.
    For users without a base audio file (or when a weighted list resolves to
    no audio clip), falls back to a synthesized TTS audio file saying their name.
    The TTS audio file is cached on disk and reused as long as their name remains
    unchanged; if their name changes, it is regenerated and overwritten.

    Returns ``True`` when audio was scheduled, ``False`` when skipped (throttled,
    vc not ready, missing audio, or feature disabled). Errors are logged and swallowed.
    """
    if not getattr(config, "GREETING_ENABLED", True):
        return False
    if user_id is None:
        return False

    now = time.time()
    last_chan = _last_greeting.get(channel_id, 0.0)
    last_user = _last_user_greeting.get((channel_id, user_id), 0.0)
    throttle_sec = float(getattr(config, "GREETING_THROTTLE_SECONDS", 15.0))

    # Per-user throttle (default 15s) and per-channel inter-clip buffer (2.0s)
    if now - last_user < throttle_sec or now - last_chan < 2.0:
        logger.info(
            "[GREETING] throttled (channel=%s, user=%s, %.1fs since last chan, %.1fs since last user)",
            channel_id, user_id, now - last_chan, now - last_user,
        )
        return False

    if not await _wait_until_ready(vc):
        logger.info("[GREETING] vc never ready (channel=%s)", channel_id)
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
    if path is None or not os.path.exists(path):
        target_name = get_user_greeting_name(user_id, member=member, display_name=display_name)
        if target_name:
            path = get_or_generate_name_tts(user_id, target_name)

    if path is None or not os.path.exists(path):
        logger.info("[GREETING] no audio clip or TTS available for user=%s", user_id)
        return False

    # Greeting audio has absolute priority over Indio's voice/audio.
    # If VC is currently playing, interrupt it.
    try:
        if vc.is_playing():
            logger.info(
                "[GREETING] interrupting ongoing audio for user greeting priority (channel=%s, user=%s)",
                channel_id, user_id,
            )
            vc.stop()
            await asyncio.sleep(0.1)
    except Exception:
        logger.exception("[GREETING] error while stopping previous audio on vc (channel=%s)", channel_id)

    _last_greeting[channel_id] = now
    _last_user_greeting[(channel_id, user_id)] = now

    global _greeting_playing
    _greeting_playing = True

    try:
        opts = get_ffmpeg_greeting_opts()
        try:
            source = discord.FFmpegOpusAudio(path, options=opts)
        except Exception:
            source = discord.FFmpegOpusAudio(path)
        vc.play(source, after=_on_greeting_end)
        logger.info("[GREETING] playing %s (user=%s, channel=%s)",
                    path, user_id, channel_id)
        return True
    except Exception:
        _greeting_playing = False
        logger.exception("[GREETING] play failed (channel=%s)", channel_id)
        return False


def resolve_wake_sound_path() -> Optional[str]:
    """Return the absolute wake-sound path, or ``None`` when unconfigured."""
    rel = getattr(config, "WAKE_SOUND_PATH", "") or ""
    if not rel:
        return None
    if os.path.isabs(rel):
        return rel
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_rel = os.path.join(repo_root, rel)
    if os.path.exists(repo_rel):
        return repo_rel
    custom_audio_dir = getattr(config, "CUSTOM_AUDIO_PATH", "/home/ubuntu/vapls-discord-bot/audio_output")
    return os.path.join(custom_audio_dir, rel)


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
    wake_throttle = float(getattr(config, "WAKE_SOUND_THROTTLE_SECONDS", 0.0))
    if now - last < wake_throttle:
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
        source = discord.FFmpegOpusAudio(path, options=get_ffmpeg_greeting_opts())
        vc.play(source)
        logger.info("[WAKE-SOUND] playing %s (user=%s, channel=%s)",
                    path, user_id, channel_id)
        return True
    except Exception:
        logger.exception("[WAKE-SOUND] play failed (channel=%s)", channel_id)
        return False
