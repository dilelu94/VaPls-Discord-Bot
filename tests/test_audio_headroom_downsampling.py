import audioop
import struct
import sys
import types
from unittest.mock import MagicMock, patch
import pytest

if "discord.ext.voice_recv" not in sys.modules:
    class MockVoiceRecv(MagicMock):
        class AudioSink: pass
    sys.modules["discord.ext.voice_recv"] = MockVoiceRecv()
if "discord.voice_state" not in sys.modules:
    mock_vs = types.ModuleType("discord.voice_state")
    mock_vs.VoiceConnectionState = MagicMock()
    sys.modules["discord.voice_state"] = mock_vs

from userbot import config
from userbot import bot as userbot_bot


def test_apply_headroom_attenuation():
    """Verify apply_headroom scales 16-bit PCM amplitudes by HEADROOM_FACTOR to prevent clipping."""
    # 4 signed 16-bit samples near the maximum range
    raw_samples = struct.pack("<4h", 30000, -30000, 10000, -10000)

    # Default HEADROOM_FACTOR is 0.85
    attenuated = userbot_bot.apply_headroom(raw_samples)
    unpacked = struct.unpack("<4h", attenuated)

    assert unpacked[0] == int(30000 * 0.85)
    assert unpacked[1] == int(-30000 * 0.85)
    assert unpacked[2] == int(10000 * 0.85)
    assert unpacked[3] == int(-10000 * 0.85)


def test_apply_headroom_bypass_when_disabled(monkeypatch):
    """Verify apply_headroom bypasses processing when HEADROOM_FACTOR is 1.0 or non-positive."""
    raw_samples = struct.pack("<2h", 20000, -20000)

    monkeypatch.setattr(userbot_bot.config, "HEADROOM_FACTOR", 1.0)
    assert userbot_bot.apply_headroom(raw_samples) == raw_samples

    monkeypatch.setattr(userbot_bot.config, "HEADROOM_FACTOR", 0.0)
    assert userbot_bot.apply_headroom(raw_samples) == raw_samples

    assert userbot_bot.apply_headroom(b"") == b""


def test_resample_48k_to_16k_dimensions():
    """Verify resample_48k_to_16k converts 20ms of 48kHz mono PCM to 20ms of 16kHz mono PCM."""
    # 20ms of 48kHz mono = 960 samples * 2 bytes = 1920 bytes
    input_48k = b"\x00\x00" * 960

    output_16k, state = userbot_bot.resample_48k_to_16k(input_48k)

    # 20ms of 16kHz mono = 320 samples * 2 bytes = 640 bytes
    assert len(output_16k) == 640
    assert userbot_bot.resample_48k_to_16k(b"")[0] == b""


def test_resample_48k_to_16k_anti_aliasing(monkeypatch):
    """Verify soxr filters frequencies above the 8kHz Nyquist cutoff, eliminating aliasing."""
    if not userbot_bot._HAS_SOXR or userbot_bot.np is None:
        pytest.skip("soxr or numpy not available in current environment")

    import numpy as np

    # Generate 100ms of a 10kHz tone at 48kHz (Nyquist is 8kHz at 16kHz output)
    t = np.linspace(0, 0.1, 4800, endpoint=False)
    tone_10k = (np.sin(2 * np.pi * 10000 * t) * 10000).astype(np.int16).tobytes()

    # Ratecv decimation (no low-pass filter -> full aliasing)
    ratecv_out, _ = audioop.ratecv(tone_10k, 2, 1, 48000, 16000, None)
    ratecv_rms = audioop.rms(ratecv_out, 2)

    # soxr anti-aliasing resampling
    soxr_out, _ = userbot_bot.resample_48k_to_16k(tone_10k)
    soxr_rms = audioop.rms(soxr_out, 2)

    # With anti-aliasing, energy of the 10kHz tone should be drastically attenuated (> 20 dB reduction)
    assert ratecv_rms > 5000
    assert soxr_rms < 1000
    assert soxr_rms < ratecv_rms * 0.2


def test_resample_48k_to_16k_fallback_audioop(monkeypatch):
    """Verify transparent fallback to audioop.ratecv when soxr is unavailable."""
    monkeypatch.setattr(userbot_bot, "_HAS_SOXR", False)

    input_48k = b"\x00\x00" * 960
    output_16k, state = userbot_bot.resample_48k_to_16k(input_48k)

    assert len(output_16k) == 640


def test_wakewordsink_write_applies_headroom(monkeypatch):
    """Verify WakeWordSink applies headroom attenuation to input PCM before buffering."""
    sink = userbot_bot.WakeWordSink(client_ref=MagicMock())
    monkeypatch.setattr(sink, "_start_idle_watcher_once", lambda: None)

    # Create dummy source and packet with loud PCM
    mock_source = types.SimpleNamespace(id=99999)
    # 20ms mono = 960 samples of value 30000
    loud_pcm = struct.pack("<960h", *([30000] * 960))
    mock_data = types.SimpleNamespace(pcm=loud_pcm)

    # Mock rolling audio buffer to inspect what frame was stored
    added_frames = []
    monkeypatch.setattr(
        userbot_bot._rolling_audio_buffer,
        "add_frame",
        lambda gid, uid, mono: added_frames.append(mono),
    )
    monkeypatch.setattr(userbot_bot, "_get_sink_guild_id", lambda s, src, uid: 12345)
    monkeypatch.setattr(userbot_bot, "_is_speaker_allowed", lambda gid, uid: True)

    sink.write(mock_source, mock_data)

    assert len(added_frames) == 1
    stored_frame = added_frames[0]
    unpacked_first_sample = struct.unpack("<h", stored_frame[:2])[0]
    # 30000 * 0.85 = 25500
    assert unpacked_first_sample == int(30000 * 0.85)


def test_transcribersink_write_applies_headroom(monkeypatch):
    """Verify TranscriberSink applies headroom attenuation to input PCM before downsampling."""
    sink = userbot_bot.TranscriberSink(client_ref=MagicMock())
    monkeypatch.setattr(sink, "_start_idle_watcher_once", lambda: None)

    mock_source = types.SimpleNamespace(id=99999)
    loud_pcm = struct.pack("<960h", *([30000] * 960))
    mock_data = types.SimpleNamespace(pcm=loud_pcm)

    added_frames = []
    monkeypatch.setattr(
        userbot_bot._rolling_audio_buffer,
        "add_frame",
        lambda gid, uid, mono: added_frames.append(mono),
    )
    monkeypatch.setattr(userbot_bot, "_get_sink_guild_id", lambda s, src, uid: 12345)
    monkeypatch.setattr(userbot_bot, "_is_speaker_allowed", lambda gid, uid: True)

    sink.write(mock_source, mock_data)

    assert len(added_frames) == 1
    stored_frame = added_frames[0]
    unpacked_first_sample = struct.unpack("<h", stored_frame[:2])[0]
    assert unpacked_first_sample == int(30000 * 0.85)
