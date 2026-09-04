"""Media inspector module: uses ffprobe to inspect audio and subtitle streams."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class AudioTrack:
    index: int  # relative audio index (0, 1, 2...)
    stream_index: int  # absolute container stream index
    language: str = "und"
    title: str = ""
    codec: str = ""
    channels: int = 2

    @property
    def display_name(self) -> str:
        lang_str = self.language.upper() if self.language and self.language != "und" else "Audio"
        ch_str = f"{self.channels}ch" if self.channels else ""
        codec_str = self.codec.upper() if self.codec else ""
        details = ", ".join(filter(None, [self.title, lang_str, codec_str, ch_str]))
        return f"Pista {self.index + 1}: {details or 'Desconocido'}"


@dataclass
class SubtitleTrack:
    index: int  # relative subtitle index (0, 1, 2...)
    stream_index: int  # absolute container stream index
    language: str = "und"
    title: str = ""
    codec: str = ""
    is_forced: bool = False

    @property
    def display_name(self) -> str:
        lang_str = self.language.upper() if self.language and self.language != "und" else "Subtítulo"
        forced_str = "(Forzados)" if self.is_forced else ""
        details = " ".join(filter(None, [self.title, lang_str, forced_str])).strip()
        return f"Sub {self.index + 1}: {details or 'Desconocido'}"


@dataclass
class MediaTracksInfo:
    url: str
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)

    @property
    def has_multiple_audios(self) -> bool:
        return len(self.audio_tracks) > 1

    @property
    def has_subtitles(self) -> bool:
        return len(self.subtitle_tracks) > 0


def _parse_ffprobe_json(data: dict, url: str) -> MediaTracksInfo:
    info = MediaTracksInfo(url=url)
    streams = data.get("streams", [])

    audio_idx = 0
    sub_idx = 0

    for stream in streams:
        ctype = stream.get("codec_type")
        abs_idx = stream.get("index", 0)
        tags = stream.get("tags") or {}
        # Normalize tag keys (case-insensitive)
        norm_tags = {k.lower(): v for k, v in tags.items()}

        lang = norm_tags.get("language", "und")
        title = norm_tags.get("title", "")
        codec = stream.get("codec_name", "")

        if ctype == "audio":
            channels = int(stream.get("channels", 2))
            info.audio_tracks.append(
                AudioTrack(
                    index=audio_idx,
                    stream_index=abs_idx,
                    language=lang,
                    title=title,
                    codec=codec,
                    channels=channels,
                )
            )
            audio_idx += 1
        elif ctype == "subtitle":
            disposition = stream.get("disposition") or {}
            is_forced = bool(disposition.get("forced", 0))
            info.subtitle_tracks.append(
                SubtitleTrack(
                    index=sub_idx,
                    stream_index=abs_idx,
                    language=lang,
                    title=title,
                    codec=codec,
                    is_forced=is_forced,
                )
            )
            sub_idx += 1

    return info


def _run_ffprobe_sync(url: str, timeout: float = 6.0) -> str | None:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-user_agent", "Mozilla/5.0",
        "-probesize", "5000000",
        "-analyzeduration", "5000000",
        "-i", url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", url[:100], e)
    return None


async def inspect_media_tracks(url: str, timeout: float = 6.0) -> MediaTracksInfo:
    """Inspect a media URL with ffprobe and return Audio/Subtitle tracks."""
    try:
        raw_json = await asyncio.to_thread(_run_ffprobe_sync, url, timeout)
        if raw_json:
            parsed = json.loads(raw_json)
            return _parse_ffprobe_json(parsed, url)
    except Exception as exc:
        log.warning("inspect_media_tracks error for %s: %s", url[:100], exc)

    return MediaTracksInfo(url=url)
