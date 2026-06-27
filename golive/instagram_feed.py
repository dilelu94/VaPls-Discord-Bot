"""Instagram reels feed fetching using instagrapi.

Provides InstagramFeed — a queue-backed reel fetcher with session persistence.
Used by InstagramReelPlayer to implement infinite-scroll reel streaming.

PENDIENTE: necesita INSTAGRAM_USER / INSTAGRAM_PASS en golive/.env y una
cuenta de Instagram creada para el userbot (no usar la personal).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

INSTAGRAPI_AVAILABLE = False
try:
    from instagrapi import Client
    from instagrapi.exceptions import LoginRequired, ClientLoginRequired

    INSTAGRAPI_AVAILABLE = True
except ImportError:
    pass


class InstagramFeed:
    """Manages Instagram login, session persistence, and reel feed queuing.

    Call :meth:`login` once, then :meth:`next_reel_url` repeatedly to consume
    reels from the user's timeline feed.  The queue is refilled automatically
    when it runs low.
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self._queue: list[dict] = []
        self._logged_in = False

    @property
    def available(self) -> bool:
        return self._logged_in and self._client is not None

    def login(self) -> bool:
        """Log in to Instagram or resume a saved session.

        Reads INSTAGRAM_USER / INSTAGRAM_PASS from the environment and persists
        the session to INSTAGRAM_SESSION_FILE (default
        ``golive/data/instagram_session.json``).
        """
        if not INSTAGRAPI_AVAILABLE:
            log.error("instagrapi no instalado — pip install instagrapi")
            return False

        user = os.environ.get("INSTAGRAM_USER", "").strip()
        password = os.environ.get("INSTAGRAM_PASS", "").strip()
        if not user or not password:
            log.error("INSTAGRAM_USER / INSTAGRAM_PASS no están configuradas en el .env")
            return False

        session_path = os.environ.get(
            "INSTAGRAM_SESSION_FILE",
            str(Path(__file__).parent / "data" / "instagram_session.json"),
        )
        Path(session_path).parent.mkdir(parents=True, exist_ok=True)

        self._client = Client()
        self._client.delay_range = [1, 3]

        if os.path.exists(session_path):
            try:
                self._client.load_settings(session_path)
                self._client.get_timeline_feed()
                self._logged_in = True
                log.info("Instagram: sesión reanudada")
                return True
            except Exception as e:
                log.warning("Instagram: sesión guardada expiró (%s), re-login", e)

        try:
            self._client.login(user, password)
            self._client.dump_settings(session_path)
            self._logged_in = True
            log.info("Instagram: logueado como %s", user)
            return True
        except Exception as e:
            log.error("Instagram: login falló: %s", e)
            self._client = None
            return False

    def next_reel_url(self) -> Optional[str]:
        """Return the next reel video URL, refilling the queue if needed."""
        if not self._logged_in or self._client is None:
            return None
        if not self._queue:
            self._refill()
        if not self._queue:
            return None
        reel = self._queue.pop(0)
        return reel.get("url")

    def _refill(self, amount: int = 10) -> None:
        """Fetch more reels from the timeline feed."""
        if self._client is None:
            return
        try:
            feed = self._client.get_timeline_feed()
        except (LoginRequired, ClientLoginRequired):
            log.warning("Instagram: sesión expiró, re-login")
            if not self.login():
                return
            feed = self._client.get_timeline_feed()
        except Exception as e:
            log.error("Instagram: feed fetch falló: %s", e)
            return

        reels = []
        for item in feed.get("feed_items", []):
            if len(reels) >= amount:
                break
            media = item.get("media_or_ad") or item.get("media") or {}
            if media.get("media_type") != 2:
                continue
            if media.get("product_type") not in ("clips", "feed"):
                continue
            versions = media.get("video_versions") or []
            video_url = versions[0].get("url") if versions else None
            if not video_url:
                continue
            reels.append(
                {
                    "url": video_url,
                    "pk": media.get("pk", ""),
                    "code": media.get("code", ""),
                }
            )

        self._queue.extend(reels)
        log.info("Instagram: %d reels obtenidos (cola=%d)", len(reels), len(self._queue))

        if not reels:
            self._refill_explore(amount)

    def _refill_explore(self, amount: int = 10) -> None:
        """Fallback: fetch reels from the explore/recommended feed."""
        if self._client is None:
            return
        try:
            explore = self._client.explore()
        except Exception as e:
            log.warning("Instagram: explore falló: %s", e)
            return

        reels = []
        for item in explore:
            if len(reels) >= amount:
                break
            media = item.get("media") or item
            if media.get("media_type") != 2:
                continue
            versions = media.get("video_versions") or []
            video_url = versions[0].get("url") if versions else None
            if not video_url:
                continue
            reels.append(
                {
                    "url": video_url,
                    "pk": media.get("pk", ""),
                    "code": media.get("code", ""),
                }
            )

        self._queue.extend(reels)
        log.info("Instagram: %d explore reels obtenidos (cola=%d)", len(reels), len(self._queue))
