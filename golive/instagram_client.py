"""Instagram API client using aiograpi for Reels feed discovery and extraction.

Provides a singleton-managed async wrapper around ``aiograpi.Client`` that
authenticates via session cookies from ``instagram_cookies.txt`` and persists
the session to ``instagram_session.json`` for reuse across restarts.

Usage:
    from instagram_client import InstagramClient

    cl = await InstagramClient.get()
    reels = await cl.discover(amount=10)
    info = await cl.reel_info("https://www.instagram.com/reel/XXXXX/")
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_SESSION_PATH = os.path.join(os.path.dirname(__file__), "instagram_session.json")


class ReelInfo(dict):
    shortcode: str
    page_url: str
    video_url: str
    title: str


class InstagramClient:
    """Async wrapper around ``aiograpi.Client`` for Reels feed operations.

    Uses ``instagram_cookies.txt`` (sessionid cookie) for authentication,
    persists the device fingerprint + session to ``instagram_session.json``
    so repeated logins don't flag the account.
    """

    _instance: Optional["InstagramClient"] = None

    def __init__(self) -> None:
        self._cl: Optional["Client"] = None  # type: ignore[name-defined]
        self._ready = False

    @classmethod
    async def get(cls) -> "InstagramClient":
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._ensure()
        elif not cls._instance._ready:
            await cls._instance._ensure()
        return cls._instance

    async def _ensure(self) -> None:
        if self._ready:
            return
        try:
            from aiograpi import Client
        except ImportError:
            log.error("aiograpi not installed — run: pip install aiograpi")
            self._ready = False
            return

        cl = Client()
        self._cl = cl

        # Try loading saved session first
        loaded = None
        if os.path.exists(_SESSION_PATH):
            try:
                loaded = cl.load_settings(_SESSION_PATH)
            except Exception as e:
                log.warning("[INSTA-CLIENT] Error loading session: %s", e)

        if loaded:
            cl.set_settings(loaded)
            self._ready = True
            log.info("[INSTA-CLIENT] Session loaded from %s", _SESSION_PATH)
            return

        # Fall back to sessionid from cookies file
        sessionid = self._get_sessionid_from_cookies()
        if sessionid:
            try:
                await cl.login_by_sessionid(sessionid)
                cl.dump_settings(_SESSION_PATH)
                self._ready = True
                log.info("[INSTA-CLIENT] Logged in via sessionid, session saved")
                return
            except Exception as e:
                log.warning("[INSTA-CLIENT] sessionid login failed: %s", e)
        else:
            log.warning("[INSTA-CLIENT] No sessionid found in cookies file")

        self._ready = False

    def _get_sessionid_from_cookies(self) -> str | None:
        from ytdlp import _get_instagram_cookies_path
        import http.cookiejar

        path = _get_instagram_cookies_path()
        if not path:
            return None
        try:
            cj = http.cookiejar.MozillaCookieJar(path)
            cj.load()
            for c in cj:
                if c.name == "sessionid" and c.value:
                    return c.value
        except Exception as e:
            log.warning("[INSTA-CLIENT] Error reading cookies: %s", e)
        return None

    async def discover(self, amount: int = 10) -> list[dict]:
        """Fetch reels from the Reels tab feed.

        Returns up to ``amount`` entries, each with ``shortcode``,
        ``page_url``, ``video_url``, and ``title``.
        Returns empty list if not authenticated or on error.
        """
        if not self._ready or not self._cl:
            log.warning("[INSTA-CLIENT] Not ready, can't discover")
            return []

        try:
            medias = await self._cl.reels(amount=amount)
        except Exception as e:
            log.warning("[INSTA-CLIENT] reels() failed: %s", e)
            return []

        results: list[dict] = []
        for m in medias:
            if m.media_type != 2:
                continue
            if not m.code:
                continue
            results.append({
                "shortcode": m.code,
                "page_url": f"https://www.instagram.com/reel/{m.code}/",
                "video_url": m.video_url,
                "title": (m.caption_text or "Instagram Reel")[:200],
            })
        return results

    async def reel_info(self, url_or_shortcode: str) -> dict | None:
        """Get info for a single reel by page URL or shortcode.

        Returns ``{shortcode, page_url, video_url, title}`` or ``None``
        if the reel doesn't exist, isn't a video, or auth fails.
        """
        if not self._ready or not self._cl:
            log.warning("[INSTA-CLIENT] Not ready, can't get reel info")
            return None

        from aiograpi import Client as ClientCls

        try:
            if url_or_shortcode.startswith("http"):
                pk = ClientCls.media_pk_from_url(url_or_shortcode)
            else:
                pk = ClientCls.media_pk_from_code(url_or_shortcode)
        except Exception as e:
            log.warning("[INSTA-CLIENT] Error extracting media PK: %s", e)
            return None

        try:
            media = await self._cl.media_info(pk)
        except Exception as e:
            log.warning("[INSTA-CLIENT] media_info() failed: %s", e)
            return None

        if not media or media.media_type != 2:
            return None

        return {
            "shortcode": media.code,
            "page_url": f"https://www.instagram.com/reel/{media.code}/",
            "video_url": media.video_url,
            "title": (media.caption_text or "Instagram Reel")[:200],
        }
