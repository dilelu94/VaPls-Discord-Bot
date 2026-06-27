"""Instagram reels feed fetching using instaloader.

Provides InstagramReelFeed — a queue-backed reel URL fetcher that uses
instaloader (scraping Instagram's GraphQL API) to discover reel page
URLs from an Instagram hashtag or user profile.  Auth is via the shared
cookies.txt session cookies.

The feed returns reel *page* URLs (e.g.
``https://www.instagram.com/reel/<shortcode>/``) which are then
extracted by yt-dlp for video+audio DASH URLs in InstagramReelPlayer.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class InstagramReelFeed:
    """Queue-backed reel URL feed using instaloader.

    Refills from a configurable Instagram source URL (default:
    explore/tags/reels).  Each call to :meth:`next_reel_url` returns a
    reel *page* URL ready for yt-dlp extraction.
    """

    def __init__(self, source_url: str | None = None) -> None:
        self._source: str = (
            source_url
            or os.environ.get(
                "INSTAGRAM_REEL_SOURCE",
                "https://www.instagram.com/explore/tags/reels/",
            )
        )
        self._queue: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def next_reel_url(self) -> Optional[str]:
        """Return the next reel page URL, refilling the queue if needed."""
        if not self._queue:
            self._refill()
        if not self._queue:
            return None
        return self._queue.pop(0)

    def _refill(self, amount: int = 10) -> None:
        """Fetch more reel URLs from the Instagram source via instaloader."""
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
