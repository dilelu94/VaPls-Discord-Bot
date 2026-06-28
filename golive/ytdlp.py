"""yt-dlp download and live-stream extraction helpers.

Used by the TV cog to serve `!play <URL>`: ``_yt_extract_live_url`` returns a
direct stream URL for live broadcasts (metadata only, no download), while
``_yt_download`` downloads VODs into a temp dir for streaming, with cleanup
helpers for the temp directory.

Instagram feed discovery uses instaloader (preferred, with Chrome 150 UA)
or falls back to yt-dlp flat-playlist extraction.  A persistent cache
(``data/reel_cache.json``) keeps the reel queue alive across rate-limits
and restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_CHROME_150_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

_REEL_CACHE_PATH: str | None = None


def _set_reel_cache_path(path: str) -> None:
    global _REEL_CACHE_PATH
    _REEL_CACHE_PATH = path


def _reel_cache_shortcodes() -> list[str]:
    if not _REEL_CACHE_PATH:
        return []
    try:
        import json
        with open(_REEL_CACHE_PATH) as f:
            data = json.load(f)
        return data.get("shortcodes", [])
    except Exception:
        return []


def _save_reel_cache(shortcodes: list[str]) -> None:
    if not _REEL_CACHE_PATH:
        return
    try:
        import json
        from datetime import datetime
        data = {
            "shortcodes": shortcodes[:200],
            "last_updated": datetime.now().isoformat(),
        }
        os.makedirs(os.path.dirname(_REEL_CACHE_PATH), exist_ok=True)
        with open(_REEL_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("cache save fallo: %s", e)


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
            "format": "bestvideo[vcodec!*=av01]+bestaudio/best[vcodec!*=av01]",
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


def _extract_instagram_sync(url: str) -> dict | None:
    """Return ``{video_url, audio_url, title}`` for an Instagram reel (sync).

    Instagram delivers audio and video as separate DASH streams, so we
    return both URLs.  The caller passes them as ``(-i video -i audio)``
    to FFmpeg.  This is the synchronous version for use in player threads.

    Returns ``None`` if extraction fails.
    """
    import yt_dlp

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

    if audio_url == video_url:
        audio_url = None

    return {
        "video_url": video_url,
        "audio_url": audio_url,
        "title": title,
    }


async def _yt_extract_instagram(url: str) -> dict | None:
    """Return ``{video_url, audio_url, title}`` for an Instagram reel.

    Instagram delivers audio and video as separate DASH streams, so we
    return both URLs.  The caller passes them as ``(-i video -i audio)``
    to FFmpeg.

    Returns ``None`` if extraction fails.
    """
    return await asyncio.to_thread(_extract_instagram_sync, url)


def _instaloader_reel_feed_urls(url: str, limit: int = 20) -> list[str]:
    """Return up to ``limit`` reel page URLs using instaloader.

    Uses instaloader (scraping Instagram's public GraphQL API) to discover
    reel URLs from a hashtag page or user profile.  Requires valid session
    cookies in ``cookies.txt``.

    Sets Chrome 150 user-agent to avoid rate-limiting by Instagram.

    Tries multiple feed sources in order:
    1. ``get_feed_posts()`` — logged-in home feed (different endpoint)
    2. ``Hashtag.get_posts()`` — tag page with SectionIterator
    3. ``Hashtag.get_posts_resumable()`` — tag page with GraphQL (current)
    4. ``Profile.from_username().get_posts()`` — user profile

    Falls back to yt-dlp if instaloader is not available or all sources fail.
    Results are saved to the persistent reel cache on success.
    """
    try:
        import http.cookiejar
        from urllib.parse import urlparse

        import instaloader
    except ImportError:
        log.warning("instaloader not installed, falling back to yt-dlp")
        return _ytdlp_reel_feed_urls(url, limit)

    def _build_loader() -> instaloader.Instaloader | None:
        try:
            L = instaloader.Instaloader(quiet=True)
            L.context.user_agent = _CHROME_150_UA
            cookies_path = _get_cookies_path()
            if cookies_path:
                cj = http.cookiejar.MozillaCookieJar(cookies_path)
                cj.load()
                L.context.update_cookies(cj)
            return L
        except Exception as e:
            log.warning("instaloader init falló: %s", e)
            return None

    def _take_reels(posts, limit: int) -> list[str]:
        urls = []
        for post in posts:
            if post.is_video and len(urls) < limit:
                urls.append(f"https://www.instagram.com/reel/{post.shortcode}/")
            if len(urls) >= limit:
                break
        return urls

    L = _build_loader()
    if not L:
        return _ytdlp_reel_feed_urls(url, limit)

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    tag = None
    if "/tags/" in path or "/explore/tags/" in path:
        tag = path.rsplit("/tags/", 1)[-1].split("/")[0]
    username = path.strip("/") if path.count("/") == 1 and path.strip("/") else None

    # Strategy 1: logged-in home feed (different endpoint)
    all_urls: list[str] = []
    try:
        log.info("[INSTALOADER] Trying get_feed_posts() limit=%d", limit)
        feed_posts = L.get_feed_posts()
        all_urls = _take_reels(feed_posts, limit)
        if all_urls:
            log.info("[INSTALOADER] feed: %d reels", len(all_urls))
    except Exception as e:
        log.warning("get_feed_posts falló: %s", e)

    # Strategy 2: Hashtag.get_posts() — SectionIterator
    if not all_urls and tag:
        try:
            log.info("[INSTALOADER] Trying Hashtag.get_posts() tag=%s", tag)
            hashtag = instaloader.Hashtag.from_name(L.context, tag)
            posts = hashtag.get_posts()
            all_urls = _take_reels(posts, limit)
            if all_urls:
                log.info("[INSTALOADER] get_posts: %d reels", len(all_urls))
        except Exception as e:
            log.warning("Hashtag.get_posts falló: %s", e)

    # Strategy 3: Hashtag.get_posts_resumable()
    if not all_urls and tag:
        try:
            log.info("[INSTALOADER] Trying Hashtag.get_posts_resumable() tag=%s", tag)
            hashtag = instaloader.Hashtag.from_name(L.context, tag)
            posts = hashtag.get_posts_resumable()
            all_urls = _take_reels(posts, limit)
            if all_urls:
                log.info("[INSTALOADER] get_posts_resumable: %d reels", len(all_urls))
        except Exception as e:
            log.warning("get_posts_resumable falló: %s", e)

    # Strategy 4: user profile
    if not all_urls and username:
        try:
            log.info("[INSTALOADER] Trying profile.get_posts() user=%s", username)
            profile = instaloader.Profile.from_username(L.context, username)
            posts = profile.get_posts()
            all_urls = _take_reels(posts, limit)
            if all_urls:
                log.info("[INSTALOADER] profile: %d reels", len(all_urls))
        except Exception as e:
            log.warning("profile.get_posts falló: %s", e)

    if not all_urls:
        log.warning("instaloader: todas las fuentes fallaron, usando yt-dlp")
        return _ytdlp_reel_feed_urls(url, limit)

    # Save to persistent cache
    try:
        cached = _reel_cache_shortcodes()
        new_shortcodes = [u.rstrip("/").rsplit("/", 1)[-1] for u in all_urls]
        merged = list(dict.fromkeys(new_shortcodes + cached))[:200]
        _save_reel_cache(merged)
    except Exception as e:
        log.warning("cache save falló: %s", e)

    return all_urls


def _ytdlp_reel_feed_urls(url: str, limit: int = 20) -> list[str]:
    """Fallback: use yt-dlp's flat playlist to discover reel URLs."""
    import yt_dlp

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": limit,
    }
    cookies = _get_cookies_path()
    if cookies:
        opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info or not info.get("entries"):
        return []

    urls = []
    for entry in info["entries"]:
        if not entry:
            continue
        entry_url = entry.get("url") or entry.get("webpage_url") or ""
        if entry_url and "instagram.com" in entry_url:
            urls.append(entry_url)
        if len(urls) >= limit:
            break

    return urls


def _instagram_reel_feed_urls(url: str, limit: int = 20) -> list[str]:
    """Return up to ``limit`` reel page URLs from an Instagram page.

    Uses instaloader (preferred) or yt-dlp (fallback) to list entries
    from a profile, hashtag, or explore page.  Returns reel URLs like
    ``https://www.instagram.com/reel/<shortcode>/``.

    The source ``url`` is configurable via ``INSTAGRAM_REEL_SOURCE`` env var
    (default ``https://www.instagram.com/explore/tags/reels/``).  Any page
    that instaloader understands will work: a user profile
    (``https://www.instagram.com/username/``) or a hashtag
    (``https://www.instagram.com/explore/tags/tag/``).
    """
    return _instaloader_reel_feed_urls(url, limit)
