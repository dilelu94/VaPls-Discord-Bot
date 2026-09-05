"""Pure-Python PCM utilities used by the userbot voice-reply recorder.

These live in their own module so behavioral tests can exercise them
without importing ``userbot.bot`` (which pulls in VOSK, voice_recv, and a
Discord user-token client at import time).
"""
from __future__ import annotations

import asyncio
import audioop
from typing import Iterable, Tuple


# voice_recv delivers 48 kHz stereo 16-bit PCM frames; we mix down to mono.
INPUT_SAMPLE_RATE = 48000
INPUT_WIDTH = 2  # 16-bit samples → 2 bytes
OUTPUT_CHANNELS = 1


def mix_pcm_frames(frames: Iterable[Tuple[float, bytes]], max_seconds: float,
                   sample_rate: int = INPUT_SAMPLE_RATE,
                   width: int = INPUT_WIDTH) -> bytes:
    """Mix timestamped mono PCM frames into one fixed-length buffer.

    Args:
        frames: Iterable of ``(offset_seconds, mono_pcm_bytes)`` tuples.
            The offset is measured from the start of the recording window.
        max_seconds: Total length of the output buffer.
        sample_rate: PCM sample rate.
        width: PCM sample width in bytes; only 2 is supported by
            :func:`audioop.add`.

    Returns:
        A bytes object of length ``int(max_seconds * sample_rate) * width``
        with each frame summed into its position (saturated). Gaps stay as
        silence.
    """
    total_samples = int(max_seconds * sample_rate)
    total_bytes = total_samples * width
    buf = bytearray(total_bytes)
    for offset, chunk in frames:
        if not chunk:
            continue
        byte_offset = int(offset * sample_rate) * width
        byte_offset -= byte_offset % width
        if byte_offset >= total_bytes:
            continue
        end = byte_offset + len(chunk)
        if end > total_bytes:
            chunk = chunk[: total_bytes - byte_offset]
            end = total_bytes
        if not chunk:
            continue
        existing = bytes(buf[byte_offset:end])
        # audioop.add saturates on overflow, which is what we want when
        # multiple speakers overlap.
        mixed = audioop.add(existing, chunk, width)
        buf[byte_offset:end] = mixed
    return bytes(buf)


def trim_trailing_silence(pcm: bytes, *,
                          sample_rate: int = INPUT_SAMPLE_RATE,
                          width: int = INPUT_WIDTH,
                          threshold: int = 250,
                          window_ms: int = 100) -> bytes:
    """Slice off trailing silence so the reply audio is tight.

    Args:
        pcm: Mono 16-bit PCM bytes.
        sample_rate: Sample rate of ``pcm``.
        width: Sample width in bytes.
        threshold: RMS threshold below which a window is treated as silence.
        window_ms: Granularity of the silence scan.

    Returns:
        PCM truncated just past the last non-silent window. Empty bytes if
        the entire buffer was below the threshold.
    """
    if not pcm:
        return pcm
    window_bytes = max(width, (sample_rate * width * window_ms) // 1000)
    last_voice_end = 0
    for i in range(0, len(pcm), window_bytes):
        window = pcm[i:i + window_bytes]
        if len(window) < width:
            continue
        if audioop.rms(window, width) >= threshold:
            last_voice_end = i + len(window)
    return pcm[:last_voice_end]


def has_voice(pcm: bytes, *,
              width: int = INPUT_WIDTH, threshold: int = 250) -> bool:
    """Return True when any portion of ``pcm`` exceeds ``threshold`` RMS."""
    if not pcm or len(pcm) < width:
        return False
    return audioop.rms(pcm, width) >= threshold


async def pcm_to_ogg_opus(pcm: bytes,
                          sample_rate: int = INPUT_SAMPLE_RATE) -> bytes:
    """Encode raw mono s16le PCM to OGG/Opus via ffmpeg.

    Args:
        pcm: Raw PCM bytes (mono 16-bit signed little-endian).
        sample_rate: PCM sample rate.

    Returns:
        OGG/Opus-encoded bytes.

    Raises:
        RuntimeError: If ffmpeg returns a non-zero exit code.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "64k", "-application", "voip",
        "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input=pcm)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {err.decode(errors='replace')}")
    return out


from collections import deque
import threading
import time
from typing import Dict, Deque, Tuple, Optional, List


class RollingAudioBuffer:
    """Thread-safe rolling buffer storing timestamped mono PCM frames per guild.

    Keeps frames up to ``max_seconds`` (default 600.0s = 10m).
    """

    def __init__(self, max_seconds: float = 600.0, rms_threshold: int = 15):
        self.max_seconds = max_seconds
        self.rms_threshold = rms_threshold
        # guild_id -> deque of (timestamp_sec, user_id, mono_pcm_bytes)
        self._buffers: Dict[int, Deque[Tuple[float, int, bytes]]] = {}
        self._lock = threading.Lock()

    def add_frame(self, guild_id: int, user_id: int, pcm: bytes, timestamp: Optional[float] = None) -> None:
        """Add a mono PCM frame for a guild and prune old frames."""
        if not pcm or not guild_id:
            return
        now = timestamp if timestamp is not None else time.monotonic()
        cutoff = now - self.max_seconds
        with self._lock:
            if guild_id not in self._buffers:
                self._buffers[guild_id] = deque()
            buf = self._buffers[guild_id]
            buf.append((now, user_id, pcm))
            while buf and buf[0][0] < cutoff:
                buf.popleft()

    def get_raw_frames(self, guild_id: int, duration_seconds: Optional[float] = None, now: Optional[float] = None) -> List[Tuple[float, int, bytes]]:
        """Return raw timestamped frames within the requested duration window."""
        now_ts = now if now is not None else time.monotonic()
        dur = duration_seconds if duration_seconds is not None else self.max_seconds
        cutoff = now_ts - dur
        with self._lock:
            buf = self._buffers.get(guild_id)
            if not buf:
                return []
            return [f for f in buf if f[0] >= cutoff]

    def get_clip(self, guild_id: int, duration_seconds: float = 600.0, now: Optional[float] = None, sample_rate: int = INPUT_SAMPLE_RATE, width: int = INPUT_WIDTH) -> Tuple[bytes, bool]:
        """Extract and mix audio frames for `guild_id` within the requested duration.

        Returns:
            Tuple of (mixed_mono_pcm_bytes, had_voice_boolean).
        """
        now_ts = now if now is not None else time.monotonic()
        dur = min(max(duration_seconds, 1.0), self.max_seconds)
        matching = self.get_raw_frames(guild_id, duration_seconds=dur, now=now_ts)
        if not matching:
            return b"", False

        # Verify voice activity
        voice_found = False
        for _, _, pcm in matching:
            try:
                if audioop.rms(pcm, width) >= self.rms_threshold:
                    voice_found = True
                    break
            except Exception:
                pass
        if not voice_found:
            return b"", False

        # Calculate clip time range
        min_ts = matching[0][0]
        effective_start = min_ts
        actual_duration = max(0.1, now_ts - effective_start)

        frames_with_offsets = [(ts - effective_start, pcm) for ts, _, pcm in matching]
        mixed = mix_pcm_frames(frames_with_offsets, max_seconds=actual_duration, sample_rate=sample_rate, width=width)
        trimmed = trim_trailing_silence(mixed, sample_rate=sample_rate, width=width, threshold=self.rms_threshold)
        return trimmed, True

    def clear_guild(self, guild_id: int) -> None:
        """Clear buffer for a guild."""
        with self._lock:
            self._buffers.pop(guild_id, None)

    def clear_all(self) -> None:
        """Clear all guild buffers."""
        with self._lock:
            self._buffers.clear()

