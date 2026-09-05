"""Media inspector module: uses ffprobe to inspect audio and subtitle streams."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


_LANG_MAP: dict[str, str] = {
    "es": "Español 🇲🇽/🇪🇸",
    "spa": "Español 🇲🇽/🇪🇸",
    "spanish": "Español 🇲🇽/🇪🇸",
    "en": "Inglés 🇺🇸/🇬🇧",
    "eng": "Inglés 🇺🇸/🇬🇧",
    "english": "Inglés 🇺🇸/🇬🇧",
    "ja": "Japonés 🇯🇵",
    "jpn": "Japonés 🇯🇵",
    "japanese": "Japonés 🇯🇵",
    "pt": "Portugués 🇧🇷",
    "por": "Portugués 🇧🇷",
    "portuguese": "Portugués 🇧🇷",
    "fr": "Francés 🇫🇷",
    "fra": "Francés 🇫🇷",
    "fre": "Francés 🇫🇷",
    "de": "Alemán 🇩🇪",
    "ger": "Alemán 🇩🇪",
    "deu": "Alemán 🇩🇪",
    "it": "Italiano 🇮🇹",
    "ita": "Italiano 🇮🇹",
    "ru": "Ruso 🇷🇺",
    "rus": "Ruso 🇷🇺",
    "zh": "Chino 🇨🇳",
    "zho": "Chino 🇨🇳",
    "chi": "Chino 🇨🇳",
    "ko": "Coreano 🇰🇷",
    "kor": "Coreano 🇰🇷",
}


def format_language(lang_code: str) -> str:
    if not lang_code or lang_code.lower() in ("und", "unk", "zxx", "none"):
        return "Desconocido"
    return _LANG_MAP.get(lang_code.lower(), lang_code.upper())


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
        lang_str = format_language(self.language)
        codec_str = self.codec.upper() if self.codec else ""
        ch_str = f"{self.channels}ch" if self.channels else ""

        parts = []
        if lang_str != "Desconocido":
            parts.append(lang_str)
        if self.title:
            parts.append(f"[{self.title}]")
        
        tech_specs = ", ".join(filter(None, [codec_str, ch_str]))
        if tech_specs:
            parts.append(f"({tech_specs})")

        label = " ".join(parts) if parts else (f"Audio {self.index + 1} (Principal)" if self.index == 0 else f"Audio {self.index + 1}")
        return f"Pista {self.index + 1}: {label}"


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
        lang_str = format_language(self.language)
        is_f = self.is_forced or "forced" in self.title.lower()
        forced_str = " (Solo carteles)" if is_f else " (Diálogo completo)"
        title_str = f" [{self.title}]" if self.title else ""
        
        if lang_str != "Desconocido":
            return f"Sub {self.index + 1}: {lang_str}{title_str}{forced_str}"
        elif self.title:
            return f"Sub {self.index + 1}: {self.title}{forced_str}"
        else:
            return f"Subtítulo {self.index + 1}{forced_str}"




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
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-user_agent", ua,
        "-headers", f"User-Agent: {ua}\r\n",
        "-probesize", "2000000",
        "-analyzeduration", "2000000",
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


async def extract_subtitle_file(stream_url: str, stream_index: int, timeout: float = 25.0) -> str | None:
    """Asynchronously extract a subtitle track from media URL using FFmpeg to a temporary .srt file."""
    if stream_index < 0:
        return None
    url_hash = hashlib.md5(f"{stream_url}_{stream_index}".encode()).hexdigest()[:10]
    out_path = f"/tmp/vapls_sub_{url_hash}.srt"
    
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
        
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "warning",
        "-fflags", "+fastseek+nobuffer",
        "-user_agent", ua,
        "-headers", f"User-Agent: {ua}\r\n",
        "-probesize", "1000000",
        "-analyzeduration", "1000000",
        "-i", stream_url,
        "-vn", "-an",
        "-map", f"0:{stream_index}",
        "-c:s", "srt",
        out_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            log.info("Successfully extracted subtitle stream %d to %s", stream_index, out_path)
            return out_path
    except Exception as e:
        log.warning("Failed extracting subtitle stream %d: %s", stream_index, e)
        if os.path.exists(out_path) and os.path.getsize(out_path) == 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
    
    return None

