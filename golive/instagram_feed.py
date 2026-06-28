"""Instagram reels feed fetching using instaloader.

Provides InstagramReelFeed — a queue-backed reel URL fetcher that uses
instaloader (scraping Instagram's GraphQL API) to discover reel page
URLs from the logged-in user's home feed.  Auth is via
``instagram_cookies.txt`` session cookies.

The feed returns reel *page* URLs (e.g.
``https://www.instagram.com/reel/<shortcode>/``) which are then
extracted by yt-dlp for video+audio DASH URLs in InstagramReelPlayer.

A persistent cache (``data/reel_cache.json``) keeps reels flowing even
when instaloader is rate-limited or yt-dlp's feed extractors fail.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Optional

log = logging.getLogger(__name__)


class InstagramReelFeed:
    """Queue-backed reel URL feed using instaloader + persistent cache.

    Refills from a configurable Instagram source URL (default: home feed).
    Uses instaloader's ``get_feed_posts()`` (with Chrome 150 UA) or yt-dlp
    fallback.

    A persistent cache (``data/reel_cache.json``) ensures reels keep
    playing even when all online sources fail.
    """

    def __init__(self, source_url: str | None = None) -> None:
        self._source: str = (
            source_url
            or os.environ.get(
                "INSTAGRAM_REEL_SOURCE",
                "https://www.instagram.com/",
            )
        )
        self._queue: list[str] = []
        self._cache_loaded = False
        self._cache_idx = 0
        self._cache_shuffled: list[str] = []
        self._last_refill = 0.0

        cache_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "data", "reel_cache.json")
        )
        from ytdlp import _set_reel_cache_path
        _set_reel_cache_path(cache_path)

    @property
    def available(self) -> bool:
        return True

    def prefill(self, amount: int = 10) -> None:
        """Pre-fill the reel queue synchronously.

        Safe to call from ``asyncio.to_thread`` before starting the player
        thread so instaloader's latency doesn't compete with the audio FIFO
        timeout.
        """
        if not self._queue:
            self._refill(amount)

    def next_reel_url(self) -> Optional[str]:
        """Return the next reel page URL, refilling the queue if needed."""
        if not self._queue:
            self._refill()
        if not self._queue:
            return self._from_cache()
        return self._queue.pop(0)

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

    def _refill(self, amount: int = 10) -> None:
        """Fetch more reel URLs from the Instagram source via instaloader."""
        now = time.monotonic()
        if now - self._last_refill < 60:
            log.info("Instagram refill: skip (last attempt %ds ago)", int(now - self._last_refill))
            return
        self._last_refill = now

        from ytdlp import _instagram_reel_feed_urls

        try:
            urls = _instagram_reel_feed_urls(self._source, limit=amount)
        except Exception as e:
            log.error("Instagram feed refill falló: %s", e)
            return

        if not urls:
            log.warning("Instagram feed: no se encontraron reels en %s", self._source)
            return

        self._queue.extend(urls)
        log.info(
            "Instagram feed: %d reels obtenidos de %s (cola=%d)",
            len(urls), self._source, len(self._queue),
        )
