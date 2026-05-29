"""Behavior: when the bot joins a voice channel it may play a per-user greeting.
It resolves the audio path (custom for known users, default otherwise), throttles
to once per 60s per channel, and silently skips when it can't/ shouldn't play.

The voice client, ffmpeg source, and the 2s settle-delay are all faked.
"""
import os
from unittest.mock import MagicMock

import pytest

import config
import greeting

MILA_ID = 285116759525031937          # has greeting "Mila/Milapollo.mp3" in USERS
UNKNOWN_ID = 999999999999


@pytest.fixture
def gr(tmp_path, monkeypatch):
    """Isolate greeting state and stub the heavy boundaries."""
    greeting._last_greeting.clear()
    greeting._pending_trigger_user.clear()
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path), raising=False)

    async def _no_sleep(*a, **k):
        return None
    monkeypatch.setattr(greeting.asyncio, "sleep", _no_sleep)

    ffmpeg = MagicMock(return_value="AUDIO_SOURCE")
    monkeypatch.setattr(greeting.discord, "FFmpegOpusAudio", ffmpeg)

    def _make_file(rel):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00")
        return str(path)

    return MagicMock(tmp=tmp_path, ffmpeg=ffmpeg, make_file=_make_file)


def _channel(vc, channel_id=123):
    ch = MagicMock()
    ch.id = channel_id
    ch.guild.voice_client = vc
    return ch


def _vc(*, connected=True, playing=False):
    vc = MagicMock()
    vc.is_connected.return_value = connected
    vc.is_playing.return_value = playing
    return vc


async def test_default_greeting_for_unknown_user(gr):
    gr.make_file(os.path.join("Audios", "Fish Carrot.m4a"))
    vc = _vc()
    greeting.set_pending_trigger(123, UNKNOWN_ID)
    await greeting.trigger_soundboard_entry(_channel(vc))

    vc.play.assert_called_once()
    assert gr.ffmpeg.call_args[0][0].endswith(os.path.join("Audios", "Fish Carrot.m4a"))


async def test_custom_greeting_for_known_user(gr):
    gr.make_file(os.path.join("Mila", "Milapollo.mp3"))
    vc = _vc()
    greeting.set_pending_trigger(123, MILA_ID)
    await greeting.trigger_soundboard_entry(_channel(vc))

    vc.play.assert_called_once()
    assert gr.ffmpeg.call_args[0][0].endswith(os.path.join("Mila", "Milapollo.mp3"))


async def test_throttled_within_60_seconds(gr):
    gr.make_file(os.path.join("Audios", "Fish Carrot.m4a"))
    vc = _vc()
    ch = _channel(vc)

    greeting.set_pending_trigger(123, UNKNOWN_ID)
    await greeting.trigger_soundboard_entry(ch)
    greeting.set_pending_trigger(123, UNKNOWN_ID)
    await greeting.trigger_soundboard_entry(ch)        # same channel, immediately

    assert vc.play.call_count == 1


async def test_skip_when_not_connected(gr):
    gr.make_file(os.path.join("Audios", "Fish Carrot.m4a"))
    vc = _vc(connected=False)
    await greeting.trigger_soundboard_entry(_channel(vc))
    vc.play.assert_not_called()


async def test_skip_when_already_playing(gr):
    gr.make_file(os.path.join("Audios", "Fish Carrot.m4a"))
    vc = _vc(playing=True)
    await greeting.trigger_soundboard_entry(_channel(vc))
    vc.play.assert_not_called()


async def test_skip_when_file_missing(gr):
    # No file created → nothing to play, and no exception bubbles up.
    vc = _vc()
    greeting.set_pending_trigger(123, UNKNOWN_ID)
    await greeting.trigger_soundboard_entry(_channel(vc))
    vc.play.assert_not_called()
