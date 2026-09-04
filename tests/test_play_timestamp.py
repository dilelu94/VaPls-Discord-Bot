"""Tests for start-time timestamp parsing and seeking in the /play command."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord

from playCommand import (
    parse_timestamp,
    extract_url_timestamp,
    getGuildPlayer,
    clearGuildPlayer,
)


def test_parse_timestamp_formats():
    """Verify parse_timestamp handles MM:SS, HH:MM:SS, unit strings, and numbers."""
    assert parse_timestamp("1:30") == 90.0
    assert parse_timestamp("01:15") == 75.0
    assert parse_timestamp("0:45") == 45.0
    assert parse_timestamp("1:02:30") == 3750.0
    assert parse_timestamp("90") == 90.0
    assert parse_timestamp("90.5") == 90.5
    assert parse_timestamp("90s") == 90.0
    assert parse_timestamp("1m30s") == 90.0
    assert parse_timestamp("2m") == 120.0
    assert parse_timestamp("1h2m30s") == 3750.0
    assert parse_timestamp("t=90s") == 90.0
    assert parse_timestamp("?t=1m30s") == 90.0
    assert parse_timestamp("start=120") == 120.0
    assert parse_timestamp(None) == 0.0
    assert parse_timestamp("") == 0.0
    assert parse_timestamp("invalid") == 0.0


def test_extract_url_timestamp():
    """Verify extract_url_timestamp extracts timestamp query parameters from YouTube URLs."""
    assert extract_url_timestamp("https://www.youtube.com/watch?v=abc&t=1m30s") == 90.0
    assert extract_url_timestamp("https://youtu.be/abc?t=90") == 90.0
    assert extract_url_timestamp("https://youtu.be/abc?start=120") == 120.0
    assert extract_url_timestamp("https://www.youtube.com/watch?v=abc") == 0.0
    assert extract_url_timestamp("busqueda cualquiera") == 0.0


@pytest.mark.asyncio
async def test_start_playing_current_uses_start_seconds(tmp_path, monkeypatch):
    """Verify startPlayingCurrent seeks using FFmpeg -ss when start_seconds is set on currentSong."""
    guild_id = 99999
    bot = MagicMock()
    player = getGuildPlayer(guild_id, bot)

    # Set up cached file to skip yt-dlp download
    song_id = "test_timestamp_song"
    downloads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "downloads")
    os.makedirs(downloads_dir, exist_ok=True)
    opus_path = os.path.join(downloads_dir, f"{song_id}.opus")

    # Create dummy file
    with open(opus_path, "wb") as f:
        f.write(b"OggS_dummy_audio")

    try:
        player.currentSong = {
            "id": song_id,
            "title": "Test Timestamp Song",
            "duration_string": "3:00",
            "start_seconds": 90.0,
        }

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        player.vc = mock_vc

        ffmpeg_calls = []

        def fake_ffmpeg(filepath, **kwargs):
            ffmpeg_calls.append((filepath, kwargs))
            return MagicMock()

        monkeypatch.setattr("discord.FFmpegOpusAudio", fake_ffmpeg)

        await player.startPlayingCurrent()

        assert len(ffmpeg_calls) == 1
        filepath, kwargs = ffmpeg_calls[0]
        assert "before_options" in kwargs
        assert "-ss 90.00" in kwargs["before_options"]
        assert mock_vc.play.called
    finally:
        if os.path.exists(opus_path):
            try:
                os.remove(opus_path)
            except Exception:
                pass
        clearGuildPlayer(guild_id)
