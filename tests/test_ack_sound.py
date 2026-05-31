"""Behavioral tests for the 'request received' acknowledgment blip.

Boundary mocked: the Discord voice client (a small fake) and
discord.FFmpegOpusAudio (so no real ffmpeg/file decode is needed). We assert on
the observable outcome: whether a clip was handed to the voice client to play.
"""
import discord
import pytest

import config
import soundpadCommand


class FakeVoiceClient:
    """Minimal stand-in for a connected py-cord voice client."""

    def __init__(self, playing=False):
        self._playing = playing
        self.played = []  # sources handed to play()

    def is_playing(self):
        return self._playing

    def play(self, source, *args, **kwargs):
        self.played.append(source)
        self._playing = True


@pytest.fixture(autouse=True)
def _stub_ffmpeg(monkeypatch):
    """Replace FFmpegOpusAudio with a marker so no real file/ffmpeg is touched."""
    monkeypatch.setattr(
        discord, "FFmpegOpusAudio", lambda path, *a, **k: ("ffmpeg", path)
    )


def _make_clip(tmp_path, category, filename):
    cat = tmp_path / category
    cat.mkdir(parents=True, exist_ok=True)
    (cat / filename).write_bytes(b"fake audio")


def test_plays_blip_when_idle_and_clip_configured(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "blip")
    vc = FakeVoiceClient(playing=False)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is True
    assert len(vc.played) == 1
    assert vc.played[0][1].endswith("blip.mp3")


def test_skips_when_already_playing(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "blip")
    vc = FakeVoiceClient(playing=True)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is False
    assert vc.played == []


def test_noop_when_query_empty(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "")
    vc = FakeVoiceClient(playing=False)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is False
    assert vc.played == []


def test_noop_when_no_clip_matches(tmp_path, monkeypatch):
    _make_clip(tmp_path, "memes", "blip.mp3")
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "zzzzzzzz")
    vc = FakeVoiceClient(playing=False)

    result = soundpadCommand.play_ack_clip(vc)

    assert result is False
    assert vc.played == []


def test_noop_when_vc_is_none(monkeypatch):
    monkeypatch.setattr(config, "ACK_SOUND_QUERY", "blip")
    assert soundpadCommand.play_ack_clip(None) is False
