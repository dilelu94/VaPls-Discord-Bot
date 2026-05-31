"""Behavior: when the userbot detects the wake word, it plays a short
confirmation sound on the same voice channel the speaker is sitting in,
throttled per channel and only when a path is configured.

These tests pin the observable promise — what the user hears — without
coupling to the internal call shape (no asserting on log messages or
exact ffmpeg options).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


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
            "userbot_greeting_wake", _USERBOT_DIR / "greeting.py",
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
    greeting._last_wake_sound.clear()
    yield
    greeting._last_wake_sound.clear()


@pytest.fixture(autouse=True)
def _audio_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ubcfg, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(ubcfg, "WAKE_SOUND_ENABLED", True)
    monkeypatch.setattr(ubcfg, "WAKE_SOUND_THROTTLE_SECONDS", 2.0)
    return tmp_path


def _make_vc(*, channel_id: int = 999, member_ids=(), connected=True, playing=False):
    vc = MagicMock(name="VoiceClient")
    vc.is_connected = MagicMock(return_value=connected)
    vc.is_playing = MagicMock(return_value=playing)
    vc.play = MagicMock()
    members = [SimpleNamespace(id=mid) for mid in member_ids]
    vc.channel = SimpleNamespace(id=channel_id, members=members)
    return vc


def _make_client(voice_clients):
    return SimpleNamespace(voice_clients=list(voice_clients))


def _configure_path(monkeypatch, audio_dir, *, filename="ding.mp3", create=True):
    if create:
        f = audio_dir / filename
        f.write_bytes(b"fake-audio")
    monkeypatch.setattr(ubcfg, "WAKE_SOUND_PATH", filename)
    return str(audio_dir / filename)


def _stub_ffmpeg(monkeypatch):
    monkeypatch.setattr(
        greeting.discord, "FFmpegOpusAudio",
        lambda *a, **k: SimpleNamespace(args=a, kwargs=k),
    )


async def test_plays_on_vc_where_user_is_present(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    vc = _make_vc(member_ids=[42])
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is True
    vc.play.assert_called_once()


async def test_skips_when_user_not_in_any_connected_vc(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    vc = _make_vc(member_ids=[7])  # the speaker isn't here
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is False
    vc.play.assert_not_called()


async def test_selects_correct_vc_when_user_account_is_in_multiple_guilds(
    monkeypatch, _audio_dir,
):
    """The userbot can be in many guilds. The sound must play on the channel
    that actually contains the speaker, not the first VC in the list."""
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    other_vc = _make_vc(channel_id=111, member_ids=[99])
    target_vc = _make_vc(channel_id=222, member_ids=[42])
    client = _make_client([other_vc, target_vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is True
    other_vc.play.assert_not_called()
    target_vc.play.assert_called_once()


async def test_throttle_blocks_second_play_within_window(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    vc = _make_vc(member_ids=[42])
    client = _make_client([vc])

    assert await greeting.play_wake_sound(client, user_id=42) is True
    assert await greeting.play_wake_sound(client, user_id=42) is False
    vc.play.assert_called_once()


async def test_throttle_is_per_channel(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    vc_a = _make_vc(channel_id=1, member_ids=[42])
    vc_b = _make_vc(channel_id=2, member_ids=[43])
    client = _make_client([vc_a, vc_b])

    assert await greeting.play_wake_sound(client, user_id=42) is True
    assert await greeting.play_wake_sound(client, user_id=43) is True
    vc_a.play.assert_called_once()
    vc_b.play.assert_called_once()


async def test_disabled_globally_short_circuits(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    monkeypatch.setattr(ubcfg, "WAKE_SOUND_ENABLED", False)
    vc = _make_vc(member_ids=[42])
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is False
    vc.play.assert_not_called()


async def test_empty_path_disables_feature(monkeypatch, _audio_dir):
    _stub_ffmpeg(monkeypatch)
    monkeypatch.setattr(ubcfg, "WAKE_SOUND_PATH", "")
    vc = _make_vc(member_ids=[42])
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is False
    vc.play.assert_not_called()


async def test_missing_audio_file_skipped(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir, filename="missing.mp3", create=False)
    _stub_ffmpeg(monkeypatch)
    vc = _make_vc(member_ids=[42])
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is False
    vc.play.assert_not_called()


async def test_vc_already_playing_is_skipped(monkeypatch, _audio_dir):
    _configure_path(monkeypatch, _audio_dir)
    _stub_ffmpeg(monkeypatch)
    vc = _make_vc(member_ids=[42], playing=True)
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is False
    vc.play.assert_not_called()


async def test_absolute_path_is_used_as_is(monkeypatch, _audio_dir):
    """A non-relative WAKE_SOUND_PATH must be honored verbatim, not joined
    with CUSTOM_AUDIO_PATH."""
    elsewhere = _audio_dir / "elsewhere"
    elsewhere.mkdir()
    audio = elsewhere / "ding.mp3"
    audio.write_bytes(b"fake")
    monkeypatch.setattr(ubcfg, "WAKE_SOUND_PATH", str(audio))
    _stub_ffmpeg(monkeypatch)
    vc = _make_vc(member_ids=[42])
    client = _make_client([vc])

    played = await greeting.play_wake_sound(client, user_id=42)

    assert played is True
    # Verify the absolute path reached FFmpegOpusAudio
    source = vc.play.call_args.args[0]
    assert str(audio) in source.args[0]
