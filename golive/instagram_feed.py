"""Instagram reels feed fetching using aiograpi.

Provides InstagramReelFeed — a queue-backed reel URL fetcher that uses
aiograpi (async Instagram private API) to discover reel page URLs and
direct video URLs from the logged-in user's Reels tab feed.  Auth is
via ``instagram_cookies.txt`` session cookies, persisted to
``instagram_session.json``.

The feed returns dicts with ``page_url`` and ``video_url`` so the player
can stream directly without yt-dlp extraction.  A persistent cache
(``data/reel_cache.json``) keeps reels flowing when the API is
rate-limited.
"""

from __future__ import annotations

import logging
import os
import random
from typing import Optional

log = logging.getLogger(__name__)


class InstagramReelFeed:
    """Queue-backed reel feed using aiograpi + persistent cache.

    Refills from the authenticated user's Reels tab feed via
    ``InstagramClient.discover()``.  The queue stores dicts with
    ``page_url``, ``video_url``, ``shortcode``, and ``title`` so the
    player can stream directly without yt-dlp.

    A persistent cache (``data/reel_cache.json``) of shortcodes ensures
    reels keep playing when all online sources fail.
    """

    def __init__(self) -> None:
        self._queue: list[dict] = []
        self._cache_loaded = False
        self._cache_idx = 0
        self._cache_shuffled: list[str] = []

        cache_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "data", "reel_cache.json")
        )
        from ytdlp import _set_reel_cache_path
        _set_reel_cache_path(cache_path)

    @property
    def available(self) -> bool:
        return True

    @property
    def size(self) -> int:
        return len(self._queue)

    async def async_prefill(self, amount: int = 10) -> None:
        """Pre-fill the reel queue asynchronously via aiograpi.

        Call from the async event loop before starting the player thread
        so API latency doesn't compete with the audio FIFO timeout.
        """
        from instagram_client import InstagramClient

        try:
            cl = await InstagramClient.get()
            reels = await cl.discover(amount=amount)
        except Exception as e:
            log.error("Instagram async_prefill falló: %s", e)
            return

        if not reels:
            log.warning("Instagram async_prefill: no se encontraron reels")
            return

        self._queue.extend(reels)
        log.info(
            "Instagram feed: %d reels obtenidos via aiograpi (cola=%d)",
            len(reels), len(self._queue),
        )
        self._cache_reels(reels)

    def next_reel_url(self) -> Optional[str]:
        """Return the next reel page URL (sync, for player thread).

        Reads from the in-memory queue or falls back to persistent cache.
        Returns ``None`` if all sources are exhausted.
        """
        if self._queue:
            return self._queue[0]["page_url"]
        return self._from_cache()

    def next_reel(self) -> Optional[dict]:
        """Return the next reel info dict (sync, for player thread).

        Returns ``{page_url, video_url, shortcode, title}`` or ``None``
        if all sources are exhausted.  The caller uses ``video_url``
        directly if present, falling back to yt-dlp extraction otherwise.
        """
        if self._queue:
            return self._queue.pop(0)
        cached = self._from_cache()
        if cached:
            return {"page_url": cached, "video_url": None, "shortcode": None, "title": "Instagram Reel (cache)"}
        return None

    @staticmethod
    def _cache_reels(reels: list[dict]) -> None:
        from ytdlp import _cache_reel_urls
        urls = [r["page_url"] for r in reels if r.get("page_url")]
        if urls:
            _cache_reel_urls(urls)

    def _from_cache(self) -> Optional[str]:
        from ytdlp import _reel_cache_shortcodes

        if not self._cache_loaded:
            cached = _reel_cache_shortcodes()
            if cached:
                self._cache_shuffled = list(cached)
                random.shuffle(self._cache_shuffled)
                self._cache_idx = 0
            self._cache_loaded = True

        if not self._cache_shuffled:
            return None

        if self._cache_idx >= len(self._cache_shuffled):
            self._cache_idx = 0
            random.shuffle(self._cache_shuffled)

        sc = self._cache_shuffled[self._cache_idx]
        self._cache_idx += 1
        url = f"https://www.instagram.com/reel/{sc}/"
        log.info("Instagram feed: sirviendo de cache %s (idx=%d)", sc, self._cache_idx)
        return url
