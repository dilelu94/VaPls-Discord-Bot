"""yt-dlp download and live-stream extraction helpers.

Used by the TV cog to serve `!play <URL>`: ``_yt_extract_live_url`` returns a
direct stream URL for live broadcasts (metadata only, no download), while
``_yt_download`` downloads VODs into a temp dir for streaming, with cleanup
helpers for the temp directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

def _get_cookies_path() -> str | None:
    parent_cookies = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cookies.txt"))
    if os.path.exists(parent_cookies):
        return parent_cookies
    local_cookies = os.path.abspath(os.path.join(os.path.dirname(__file__), "cookies.txt"))
    if os.path.exists(local_cookies):
        return local_cookies
    return None


def _get_extractor_args() -> dict | None:
    pot_url = os.environ.get("YT_DLP_POT_BASE_URL", "http://127.0.0.1:4416").strip()
    if pot_url:
        return {"youtubepot-bgutilhttp": {"base_url": pot_url}}
    return None


async def _yt_extract_url(url: str) -> tuple[str, str, bool] | None:
    """Return (stream_url, title, is_live) if url can be streamed directly, else None.

    Uses yt-dlp metadata only — no download — so it's fast.
    """
    import yt_dlp

    def _run() -> tuple[str, str, bool] | None:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "remote_components": ["ejs:github"],
            "format": "best",
        }
        cookies = _get_cookies_path()
        if cookies:
            opts["cookiefile"] = cookies
        ext_args = _get_extractor_args()
        if ext_args:
            opts["extractor_args"] = ext_args
            
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

        title: str = info.get("title") or "YouTube Stream"
        is_live = info.get("live_status") == "is_live"

        # HLS for live streams only (avoids YouTube's unreliable HLS for VODs).
        formats: list[dict] = info.get("formats") or []
        hls = [
            f for f in formats
            if f.get("protocol", "").startswith("m3u8")
            and f.get("url")
            and f.get("vcodec") not in (None, "none")
        ]
        if is_live and hls:
            best = max(hls, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
            return best["url"], title, is_live

        if info.get("requested_formats"):
            req = info["requested_formats"]
            if len(req) == 2:
                return (req[0]["url"], req[1]["url"]), title, is_live

        if info.get("url"):
            return info["url"], title, is_live

        return None

    return await asyncio.to_thread(_run)


async def _yt_extract_instagram(url: str) -> dict | None:
    """Return ``{video_url, audio_url, title}`` for an Instagram reel.

    Instagram delivers audio and video as separate DASH streams, so we
    return both URLs.  The caller passes them as ``(-i video -i audio)``
    to FFmpeg.

    Returns ``None`` if extraction fails.
    """
    import yt_dlp

    def _run() -> dict | None:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestvideo+bestaudio/best",
        }
        cookies = _get_cookies_path()
        if cookies:
            opts["cookiefile"] = cookies
        ext_args = _get_extractor_args()
        if ext_args:
            opts["extractor_args"] = ext_args

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

        title = info.get("title") or "Instagram Reel"
        video_url = None
        audio_url = None

        if info.get("requested_formats") and len(info["requested_formats"]) >= 2:
            req = info["requested_formats"]
            video_url = req[0].get("url")
            audio_url = req[1].get("url")
        elif info.get("url"):
            video_url = info["url"]

        if not video_url:
            return None

        return {
            "video_url": video_url,
            "audio_url": audio_url,
            "title": title,
        }

    return await asyncio.to_thread(_run)
