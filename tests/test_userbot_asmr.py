"""Tests for the userbot ASMR ambient audio playback module."""
import datetime
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


def _load_userbot_asmr():
    real_config = sys.modules.get("config")

    uc_spec = importlib.util.spec_from_file_location(
        "userbot_config", _USERBOT_DIR / "config.py",
    )
    uc = importlib.util.module_from_spec(uc_spec)
    sys.modules["config"] = uc
    uc_spec.loader.exec_module(uc)

    try:
        a_spec = importlib.util.spec_from_file_location(
            "userbot_asmr", _USERBOT_DIR / "asmr.py",
        )
        asmr_mod = importlib.util.module_from_spec(a_spec)
        a_spec.loader.exec_module(asmr_mod)
    finally:
        if real_config is not None:
            sys.modules["config"] = real_config
        else:
            sys.modules.pop("config", None)
    return asmr_mod, uc


asmr, ubcfg = _load_userbot_asmr()


@pytest.fixture(autouse=True)
def reset_asmr_module_state(tmp_path, monkeypatch):
    """Reset module-level in-memory state before each test."""
    state_file = tmp_path / "asmr_state.json"
    monkeypatch.setattr(ubcfg, "ASMR_STATE_PATH", str(state_file))
    monkeypatch.setattr(ubcfg, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(ubcfg, "ASMR_ENABLED", True)
    monkeypatch.setattr(ubcfg, "ASMR_MIN_CONNECTED_SECONDS", 1800)
    monkeypatch.setattr(ubcfg, "ASMR_MIN_VOLUME", 0.50)
    monkeypatch.setattr(ubcfg, "ASMR_MAX_VOLUME", 0.90)

    asmr._last_played_ts = 0.0
    asmr._state_loaded = False
    asmr._member_connected_since.clear()
    yield
    asmr._last_played_ts = 0.0
    asmr._state_loaded = False
    asmr._member_connected_since.clear()


def test_resolve_asmr_audio_path_missing(tmp_path):
    """Should return None when the asmr directory does not exist or has no audio files."""
    assert asmr.resolve_asmr_audio_path() is None


def test_resolve_asmr_audio_path_found(tmp_path):
    """Should find and return an audio file from the asmr directory."""
    asmr_dir = tmp_path / "asmr"
    asmr_dir.mkdir(parents=True, exist_ok=True)
    sample_file = asmr_dir / "whisper.mp3"
    sample_file.write_bytes(b"fake audio content")

    resolved = asmr.resolve_asmr_audio_path()
    assert resolved is not None
    assert os.path.basename(resolved) == "whisper.mp3"


def test_can_play_today_never_played():
    """Should allow playback when never played before."""
    assert asmr.can_play_today(now=time.time()) is True


def test_can_play_today_played_recently():
    """Should disallow playback if played within the last 24 hours."""
    now = time.time()
    asmr._last_played_ts = now - 3600  # 1 hour ago
    asmr._state_loaded = True
    assert asmr.can_play_today(now=now) is False


def test_can_play_today_played_yesterday():
    """Should allow playback if played 25 hours ago on a previous day."""
    now = time.time()
    asmr._last_played_ts = now - (25 * 3600)  # 25 hours ago
    asmr._state_loaded = True
    assert asmr.can_play_today(now=now) is True


def test_get_eligible_member_feature_disabled(monkeypatch):
    """Should return None when ASMR_ENABLED is False."""
    monkeypatch.setattr(ubcfg, "ASMR_ENABLED", False)
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    assert asmr.get_eligible_member(MagicMock(), vc) is None


def test_get_eligible_member_already_played_today():
    """Should return None if already played today."""
    now = time.time()
    asmr._last_played_ts = now - 100
    asmr._state_loaded = True
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    assert asmr.get_eligible_member(MagicMock(), vc, now=now) is None


def test_get_eligible_member_bot_muted():
    """Should return None if the bot itself is self_muted or muted in the guild."""
    now = time.time()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False

    guild_me = MagicMock()
    guild_me.voice.self_mute = True
    vc.guild.me = guild_me

    assert asmr.get_eligible_member(MagicMock(), vc, now=now) is None


def test_get_eligible_member_human_muted():
    """Should return None if the human member is self_muted or muted."""
    now = time.time()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False

    guild_me = MagicMock()
    guild_me.voice.self_mute = False
    guild_me.voice.mute = False
    guild_me.voice.self_deaf = False
    guild_me.voice.deaf = False
    vc.guild.me = guild_me

    human = MagicMock()
    human.bot = False
    human.id = 12345
    human.voice.self_mute = True  # Muted user!
    human.voice.mute = False
    human.voice.self_deaf = False
    human.voice.deaf = False

    vc.channel.id = 999
    vc.channel.members = [human]

    asmr._member_connected_since[(999, 12345)] = now - 3600  # Connected 1h ago
    assert asmr.get_eligible_member(MagicMock(), vc, now=now) is None


def test_get_eligible_member_insufficient_connection_time():
    """Should return None if human member connected for less than 30 minutes (1800s)."""
    now = time.time()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False

    guild_me = MagicMock()
    guild_me.voice.self_mute = False
    guild_me.voice.mute = False
    guild_me.voice.self_deaf = False
    guild_me.voice.deaf = False
    vc.guild.me = guild_me

    human = MagicMock()
    human.bot = False
    human.id = 12345
    human.voice.self_mute = False
    human.voice.mute = False
    human.voice.self_deaf = False
    human.voice.deaf = False

    vc.channel.id = 999
    vc.channel.members = [human]

    # Joined only 10 minutes (600s) ago
    asmr._member_connected_since[(999, 12345)] = now - 600
    assert asmr.get_eligible_member(MagicMock(), vc, now=now) is None


def test_get_eligible_member_success():
    """Should return member when human connected >= 30 minutes and neither bot nor user is muted."""
    now = time.time()
    client = MagicMock()
    client.user.id = 77777

    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False

    guild_me = MagicMock()
    guild_me.voice.self_mute = False
    guild_me.voice.mute = False
    guild_me.voice.self_deaf = False
    guild_me.voice.deaf = False
    vc.guild.me = guild_me

    human = MagicMock()
    human.bot = False
    human.id = 12345
    human.voice.self_mute = False
    human.voice.mute = False
    human.voice.self_deaf = False
    human.voice.deaf = False

    vc.channel.id = 999
    vc.channel.members = [human]

    # Joined 35 minutes (2100s) ago
    asmr._member_connected_since[(999, 12345)] = now - 2100
    eligible = asmr.get_eligible_member(client, vc, now=now)
    assert eligible == human


@pytest.mark.asyncio
async def test_play_asmr_success(tmp_path, monkeypatch):
    """Should play audio clip with randomized volume (50% to 90%) and update state."""
    asmr_dir = tmp_path / "asmr"
    asmr_dir.mkdir(parents=True, exist_ok=True)
    sample_file = asmr_dir / "soft_rain.ogg"
    sample_file.write_bytes(b"dummy audio content")

    vc = MagicMock()
    vc.is_connected.return_value = True

    with patch("discord.FFmpegOpusAudio") as mock_audio_cls:
        mock_audio_cls.return_value = MagicMock()
        played = await asmr.play_asmr(vc)
        assert played is True
        vc.play.assert_called_once()

        # Check options passed to FFmpegOpusAudio contain volume between 0.50 and 0.90
        call_kwargs = mock_audio_cls.call_args[1]
        options = call_kwargs.get("options", "")
        assert "-af \"volume=" in options
        # Extract volume float from options
        vol_str = options.split("volume=")[1].split('"')[0]
        vol_float = float(vol_str)
        assert 0.50 <= vol_float <= 0.90

        # State should be updated
        assert asmr._last_played_ts > 0
        state_file = tmp_path / "asmr_state.json"
        assert state_file.exists()
        with open(state_file, "r") as f:
            data = json.load(f)
            assert data["last_played_ts"] == asmr._last_played_ts
