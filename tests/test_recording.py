"""Behavioral tests for the userbot's PCM mixing + silence trimming.

The voice-reply recorder turns a stream of timestamped PCM chunks from
multiple Discord speakers into a single fixed-length mono buffer, then
clips off trailing silence before encoding to OGG/Opus. We pin those two
guarantees here without touching Discord or voice_recv.
"""
from __future__ import annotations

import audioop
import importlib.util
import math
import struct
from pathlib import Path

import pytest


# userbot/ has no __init__.py (its modules are loaded as top-level scripts at
# runtime), so we load recording.py directly from disk without poisoning the
# global sys.path — the main bot already has its own ``config`` module and we
# don't want ours to shadow it.
_RECORDING_PATH = (
    Path(__file__).resolve().parent.parent / "userbot" / "recording.py"
)
_spec = importlib.util.spec_from_file_location(
    "userbot_recording", _RECORDING_PATH
)
recording = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recording)

INPUT_SAMPLE_RATE = recording.INPUT_SAMPLE_RATE
INPUT_WIDTH = recording.INPUT_WIDTH
mix_pcm_frames = recording.mix_pcm_frames
trim_trailing_silence = recording.trim_trailing_silence
has_voice = recording.has_voice


def _tone(seconds: float, freq: int = 440, amplitude: int = 8000,
          sample_rate: int = INPUT_SAMPLE_RATE) -> bytes:
    """Generate a mono 16-bit PCM sine tone."""
    n = int(seconds * sample_rate)
    samples = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate))
        samples.extend(struct.pack("<h", v))
    return bytes(samples)


def _silence(seconds: float, sample_rate: int = INPUT_SAMPLE_RATE) -> bytes:
    return b"\x00\x00" * int(seconds * sample_rate)


def _samples_at(pcm: bytes, offset_seconds: float, count: int,
                sample_rate: int = INPUT_SAMPLE_RATE) -> list[int]:
    start = int(offset_seconds * sample_rate) * INPUT_WIDTH
    raw = pcm[start:start + count * INPUT_WIDTH]
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


# --------------------------------------------------------------------------
# Mixing
# --------------------------------------------------------------------------
def test_mix_places_each_frame_at_its_offset():
    """A frame's audio lands exactly at its (offset, offset+duration) slot."""
    tone = _tone(0.5)
    out = mix_pcm_frames([(1.0, tone)], max_seconds=3.0)

    # Output is exactly 3s long.
    assert len(out) == 3 * INPUT_SAMPLE_RATE * INPUT_WIDTH

    # Before the frame: silence.
    pre = out[: int(1.0 * INPUT_SAMPLE_RATE) * INPUT_WIDTH]
    assert audioop.rms(pre, INPUT_WIDTH) == 0

    # During the frame: signal present.
    during_start = int(1.0 * INPUT_SAMPLE_RATE) * INPUT_WIDTH
    during_end = int(1.5 * INPUT_SAMPLE_RATE) * INPUT_WIDTH
    assert audioop.rms(out[during_start:during_end], INPUT_WIDTH) > 1000

    # After the frame: silence again.
    post = out[during_end:]
    assert audioop.rms(post, INPUT_WIDTH) == 0


def test_mix_sums_overlapping_speakers():
    """When two speakers overlap, their amplitudes sum (saturated)."""
    a = _tone(0.5, amplitude=4000)
    b = _tone(0.5, amplitude=4000)
    out = mix_pcm_frames([(0.0, a), (0.0, b)], max_seconds=1.0)

    # The overlapping region should be louder than either solo speaker.
    overlap = out[: int(0.5 * INPUT_SAMPLE_RATE) * INPUT_WIDTH]
    assert audioop.rms(overlap, INPUT_WIDTH) > audioop.rms(a, INPUT_WIDTH)


def test_mix_clamps_frame_extending_past_buffer():
    """A frame that runs past max_seconds gets truncated, not rejected."""
    long_tone = _tone(2.0)  # longer than the window
    out = mix_pcm_frames([(0.5, long_tone)], max_seconds=1.0)

    # Buffer is still exactly 1 s.
    assert len(out) == 1 * INPUT_SAMPLE_RATE * INPUT_WIDTH
    # The audible portion (0.5 s..1.0 s) has signal.
    tail = out[int(0.5 * INPUT_SAMPLE_RATE) * INPUT_WIDTH:]
    assert audioop.rms(tail, INPUT_WIDTH) > 500


def test_mix_ignores_frame_starting_after_buffer():
    """A frame whose offset is past max_seconds simply doesn't appear."""
    tone = _tone(0.2)
    out = mix_pcm_frames([(5.0, tone)], max_seconds=1.0)
    assert audioop.rms(out, INPUT_WIDTH) == 0


def test_mix_empty_frames_yield_silent_buffer():
    out = mix_pcm_frames([], max_seconds=2.0)
    assert len(out) == 2 * INPUT_SAMPLE_RATE * INPUT_WIDTH
    assert audioop.rms(out, INPUT_WIDTH) == 0


# --------------------------------------------------------------------------
# Silence trimming
# --------------------------------------------------------------------------
def test_trim_drops_trailing_silence():
    """Trailing zero-amplitude tail is sliced off after the last voiced window."""
    pcm = _tone(1.0) + _silence(5.0)
    trimmed = trim_trailing_silence(pcm)
    # Should be close to 1 s of audio (within one trim window of 100 ms).
    expected_bytes = 1 * INPUT_SAMPLE_RATE * INPUT_WIDTH
    window_bytes = INPUT_SAMPLE_RATE * INPUT_WIDTH * 100 // 1000
    assert expected_bytes <= len(trimmed) <= expected_bytes + window_bytes


def test_trim_keeps_audio_when_there_is_no_silence():
    """A buffer that's voiced through the end isn't truncated."""
    pcm = _tone(2.0)
    trimmed = trim_trailing_silence(pcm)
    assert len(trimmed) == len(pcm)


def test_trim_returns_empty_when_input_is_all_silence():
    """Silent-only input collapses to zero bytes so we can skip the callback."""
    pcm = _silence(2.0)
    trimmed = trim_trailing_silence(pcm)
    assert trimmed == b""


def test_trim_preserves_speech_followed_by_brief_pause_then_speech():
    """A natural pause between phrases doesn't truncate the second phrase."""
    pcm = _tone(0.5) + _silence(0.3) + _tone(0.5) + _silence(2.0)
    trimmed = trim_trailing_silence(pcm)
    # Should include both tones (1.0 s + the 0.3 s pause between them).
    minimum_kept = 1.3 * INPUT_SAMPLE_RATE * INPUT_WIDTH
    assert len(trimmed) >= minimum_kept
    # But not the long trailing silence.
    assert len(trimmed) < len(pcm) - 1.0 * INPUT_SAMPLE_RATE * INPUT_WIDTH


# --------------------------------------------------------------------------
# Voice activity detection
# --------------------------------------------------------------------------
def test_has_voice_detects_loud_audio():
    assert has_voice(_tone(0.1, amplitude=8000)) is True


def test_has_voice_rejects_silence():
    assert has_voice(_silence(0.1)) is False


def test_has_voice_handles_empty_input():
    assert has_voice(b"") is False
