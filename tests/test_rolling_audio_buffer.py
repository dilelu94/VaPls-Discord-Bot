import time
import pytest
import audioop
from userbot.recording import RollingAudioBuffer, INPUT_SAMPLE_RATE, INPUT_WIDTH


def test_add_frame_and_get_clip_basic():
    buf = RollingAudioBuffer(max_seconds=600.0)
    guild_id = 12345
    user_id = 67890
    now = time.monotonic()

    # Create 0.1s of 48kHz mono voice (sine wave/non-silent PCM)
    sample_rate = INPUT_SAMPLE_RATE
    duration = 0.1
    samples = int(sample_rate * duration)
    # Generate non-zero PCM bytes
    pcm = b"\x10\x20" * samples

    buf.add_frame(guild_id, user_id, pcm, timestamp=now - 5.0)
    
    mixed_pcm, has_voice = buf.get_clip(guild_id, duration_seconds=10.0, now=now)
    assert has_voice is True
    assert len(mixed_pcm) > 0


def test_rolling_buffer_max_seconds_pruning():
    buf = RollingAudioBuffer(max_seconds=600.0)
    guild_id = 100
    user_id = 200
    now = time.monotonic()
    pcm = b"\x20\x20" * 480  # ~10ms frame

    # Add old frame 700s ago
    buf.add_frame(guild_id, user_id, pcm, timestamp=now - 700.0)
    # Add recent frame 100s ago
    buf.add_frame(guild_id, user_id, pcm, timestamp=now - 100.0)

    # Frame from 700s ago should have been pruned
    frames = buf.get_raw_frames(guild_id, now=now)
    assert len(frames) == 1
    assert frames[0][0] == now - 100.0


def test_get_clip_duration_window():
    buf = RollingAudioBuffer(max_seconds=600.0)
    guild_id = 1
    user_id = 2
    now = time.monotonic()
    pcm = b"\x30\x30" * 480

    buf.add_frame(guild_id, user_id, pcm, timestamp=now - 120.0)
    buf.add_frame(guild_id, user_id, pcm, timestamp=now - 10.0)

    # Ask for last 30s
    mixed_pcm, has_voice = buf.get_clip(guild_id, duration_seconds=30.0, now=now)
    assert has_voice is True
    # Verify raw frames in 30s window only has the frame from 10s ago
    window_frames = buf.get_raw_frames(guild_id, duration_seconds=30.0, now=now)
    assert len(window_frames) == 1
    assert window_frames[0][0] == now - 10.0


def test_multiple_guilds_isolated():
    buf = RollingAudioBuffer(max_seconds=600.0)
    now = time.monotonic()
    pcm = b"\x40\x40" * 480

    buf.add_frame(guild_id=1, user_id=10, pcm=pcm, timestamp=now - 5.0)
    buf.add_frame(guild_id=2, user_id=20, pcm=pcm, timestamp=now - 5.0)

    assert len(buf.get_raw_frames(1, now=now)) == 1
    assert len(buf.get_raw_frames(2, now=now)) == 1

    buf.clear_guild(1)
    assert len(buf.get_raw_frames(1, now=now)) == 0
    assert len(buf.get_raw_frames(2, now=now)) == 1


def test_empty_or_silence_buffer():
    buf = RollingAudioBuffer(max_seconds=600.0)
    now = time.monotonic()

    # Empty buffer
    mixed, has_voice = buf.get_clip(999, duration_seconds=10.0, now=now)
    assert mixed == b""
    assert has_voice is False

    # Silence buffer (all zeros)
    pcm_silence = b"\x00\x00" * 480
    buf.add_frame(999, 1, pcm_silence, timestamp=now - 2.0)
    mixed, has_voice = buf.get_clip(999, duration_seconds=10.0, now=now)
    assert has_voice is False
