"""Behavior: the userbot plays a per-user greeting only when ``users.USERS``
has an explicit ``greeting`` path for that user — never a default fallback —
and never twice in a row within the throttle window."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Load the userbot greeting module without polluting sys.path globally. We
# briefly register userbot/config.py under name "config" so `import config`
# inside greeting.py resolves to the userbot's config, then restore the main
# bot's config (already loaded by conftest) so other tests stay intact.
_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"


def _load_userbot_greeting():
    real_config = sys.modules.get("config")

    uc_spec = importlib.util.spec_from_file_location(
        "userbot_config", _USERBOT_DIR / "config.py",
    )
    uc = importlib.util.module_from_spec(uc_spec)
    sys.modules["config"] = uc
    uc_spec.loader.exec_module(uc)

    try:
        g_spec = importlib.util.spec_from_file_location(
            "userbot_greeting", _USERBOT_DIR / "greeting.py",
        )
        g = importlib.util.module_from_spec(g_spec)
        g_spec.loader.exec_module(g)
    finally:
        if real_config is not None:
            sys.modules["config"] = real_config
        else:
            sys.modules.pop("config", None)
    return g, uc


greeting, ubcfg = _load_userbot_greeting()


@pytest.fixture(autouse=True)
def _reset_throttle():
    greeting._last_greeting.clear()
    yield
    greeting._last_greeting.clear()


@pytest.fixture
def fake_users(monkeypatch):
    """Inject a controlled users.USERS map for the duration of the test."""
    def _set(mapping):
        monkeypatch.setattr(greeting, "_users_map", lambda: mapping)
    return _set


@pytest.fixture(autouse=True)
def _audio_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ubcfg, "CUSTOM_AUDIO_PATH", str(tmp_path))
    return tmp_path


def _make_vc(*, connected=True, playing=False):
    vc = MagicMock(name="VoiceClient")
    vc.is_connected = MagicMock(return_value=connected)
    vc.is_playing = MagicMock(return_value=playing)
    vc.play = MagicMock()
    vc.channel = SimpleNamespace(id=999)
    return vc


def test_user_with_greeting_resolves_to_absolute_path(fake_users, _audio_dir):
    fake_users({42: {"name": "Mati", "greeting": "Audios/fart.wav"}})
    path = greeting.resolve_greeting_path(42)
    assert path is not None
    assert path.endswith("Audios/fart.wav")
    assert str(_audio_dir) in path


def test_user_without_greeting_returns_none_no_default(fake_users):
    """KEY BEHAVIOR: no default fallback — users without explicit greeting do
    not trigger anything."""
    fake_users({211354006805676032: {"name": "Miles", "traits": []}})  # no greeting key
    assert greeting.resolve_greeting_path(211354006805676032) is None


def test_unknown_user_returns_none(fake_users):
    fake_users({})
    assert greeting.resolve_greeting_path(9999999) is None


def test_none_user_id_returns_none(fake_users):
    fake_users({0: {"greeting": "a.mp3"}})
    assert greeting.resolve_greeting_path(None) is None


async def test_play_skips_user_without_greeting(fake_users):
    fake_users({1: {"name": "noaudio"}})  # no greeting
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=1, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


async def test_play_invokes_vc_play_for_configured_user(
    fake_users, _audio_dir, monkeypatch,
):
    audio = _audio_dir / "Audios" / "test.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "Audios/test.mp3"}})
    # Stub FFmpegOpusAudio so we don't actually spawn ffmpeg.
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace(args=a, kwargs=k))
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is True
    vc.play.assert_called_once()


async def test_throttle_blocks_second_call_within_window(
    fake_users, _audio_dir, monkeypatch,
):
    audio = _audio_dir / "g.mp3"
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "g.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc()
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=7) is True
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=7) is False
    vc.play.assert_called_once()


async def test_throttle_is_per_channel(fake_users, _audio_dir, monkeypatch):
    audio = _audio_dir / "g.mp3"
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "g.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc()
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=1) is True
    assert await greeting.play_user_greeting(vc, user_id=42, channel_id=2) is True
    assert vc.play.call_count == 2


async def test_missing_audio_file_skipped(fake_users, monkeypatch):
    fake_users({42: {"greeting": "does/not/exist.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


async def test_already_playing_vc_skipped(fake_users, _audio_dir, monkeypatch):
    audio = _audio_dir / "g.mp3"
    audio.write_bytes(b"fake")
    fake_users({42: {"greeting": "g.mp3"}})
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio",
                        lambda *a, **k: SimpleNamespace())
    vc = _make_vc(playing=True)
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is False
    vc.play.assert_not_called()


async def test_disabled_globally_short_circuits(fake_users, monkeypatch):
    monkeypatch.setattr(ubcfg, "GREETING_ENABLED", False)
    fake_users({42: {"greeting": "g.mp3"}})
    vc = _make_vc()
    played = await greeting.play_user_greeting(vc, user_id=42, channel_id=100)
    assert played is False
    vc.play.assert_not_called()
