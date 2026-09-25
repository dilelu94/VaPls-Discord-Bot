"""Session token manager for /stream stremio and /stream anime web interface.

Generates unique, expiring session tokens for Discord users so the web app and API
are protected by one-time links generated via Discord commands.
"""

from dataclasses import asdict, dataclass
import json
import logging
import os
import time
from typing import Optional
import uuid

logger = logging.getLogger("stremio_sessions")

_STORAGE_PATH = os.path.join(os.path.dirname(__file__), "data", "stremio_sessions.json")


@dataclass
class StremioSession:
    token: str
    author_id: int
    author_name: str
    channel_id: int
    guild_id: int
    created_at: float
    expires_at: float


class StremioSessionManager:
    def __init__(self, default_ttl_hours: float = 6.0, storage_path: str = _STORAGE_PATH):
        self.sessions: dict[str, StremioSession] = {}
        self.default_ttl_hours = default_ttl_hours
        self.storage_path = storage_path
        self._load_sessions()

    def _load_sessions(self) -> None:
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            now = time.time()
            count = 0
            for item in data:
                sess = StremioSession(**item)
                if sess.expires_at > now:
                    self.sessions[sess.token] = sess
                    count += 1
            logger.info("Loaded %d active Stremio sessions from %s", count, self.storage_path)
        except Exception as e:
            logger.warning("Failed to load Stremio sessions from %s: %s", self.storage_path, e)

    def _save_sessions(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            now = time.time()
            active = [asdict(s) for s in self.sessions.values() if s.expires_at > now]
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(active, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save Stremio sessions to %s: %s", self.storage_path, e)

    def create_session(
        self,
        author_id: int,
        author_name: str,
        channel_id: int,
        guild_id: int,
        ttl_hours: Optional[float] = None,
    ) -> StremioSession:
        token = uuid.uuid4().hex
        now = time.time()
        ttl = (ttl_hours if ttl_hours is not None else self.default_ttl_hours) * 3600.0
        sess = StremioSession(
            token=token,
            author_id=author_id,
            author_name=author_name,
            channel_id=channel_id,
            guild_id=guild_id,
            created_at=now,
            expires_at=now + ttl,
        )
        self.sessions[token] = sess
        self._save_sessions()
        logger.info(
            "Created Stremio session token=%s author=%s channel=%s guild=%s expires_in=%.1fh",
            token,
            author_name,
            channel_id,
            guild_id,
            ttl / 3600.0,
        )
        return sess

    def revoke_sessions_for_guild(self, guild_id: int) -> int:
        """Revokes/expires all session tokens associated with a given guild ID."""
        to_remove = [t for t, s in self.sessions.items() if s.guild_id == guild_id]
        for t in to_remove:
            self.sessions.pop(t, None)
        if to_remove:
            self._save_sessions()
            logger.info("Revoked %d Stremio sessions for guild=%s", len(to_remove), guild_id)
        return len(to_remove)

    def get_session(self, token: str) -> Optional[StremioSession]:
        if not token:
            return None
        sess = self.sessions.get(token)
        if not sess:
            return None
        now = time.time()
        # If stream is active in guild or active session, keep token valid up to 6 hours from now
        try:
            import sys
            bot_mod = sys.modules.get("bot")
            if bot_mod and hasattr(bot_mod, "_active_sources"):
                if sess.guild_id in bot_mod._active_sources:
                    sess.expires_at = max(sess.expires_at, now + 21600.0)
        except Exception:
            pass

        if now > sess.expires_at:
            logger.info("Stremio session expired: token=%s", token)
            self.sessions.pop(token, None)
            self._save_sessions()
            return None
        return sess

    def touch_session(self, token: str, min_ttl_seconds: float = 21600.0) -> Optional[StremioSession]:
        sess = self.get_session(token)
        if not sess:
            return None
        now = time.time()
        if sess.expires_at - now < min_ttl_seconds:
            sess.expires_at = now + min_ttl_seconds
            self._save_sessions()
            logger.info("Touched/refreshed Stremio session token=%s author=%s", token, sess.author_name)
        return sess

    def validate_token(self, token: str) -> bool:
        return self.get_session(token) is not None

    def extend_session(self, token: str, duration_seconds: float) -> Optional[StremioSession]:
        sess = self.get_session(token)
        if not sess:
            return None
        now = time.time()
        ttl = max(21600.0, float(duration_seconds) + 7200.0)
        sess.expires_at = max(sess.expires_at, now + ttl)
        self._save_sessions()
        logger.info(
            "Extended Stremio session token=%s author=%s expires_in=%.1fh",
            token,
            sess.author_name,
            ttl / 3600.0,
        )
        return sess


session_manager = StremioSessionManager()


_WATCHED_STORAGE_PATH = os.path.join(os.path.dirname(__file__), "data", "stremio_watched.json")


class WatchedManager:
    """Manages persistent watch history for Stremio episodes and movies."""

    def __init__(self, storage_path: str = _WATCHED_STORAGE_PATH):
        self.storage_path = storage_path
        self.watched: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.storage_path):
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                self.watched = json.load(f)
        except Exception as e:
            logger.warning("Failed to load watched history from %s: %s", self.storage_path, e)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.watched, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save watched history to %s: %s", self.storage_path, e)

    def get_all(self) -> dict[str, float]:
        return dict(self.watched)

    def set_watched(self, key: str, state: bool = True) -> dict[str, float]:
        if not key:
            return self.get_all()
        clean_key = str(key).strip()
        if not clean_key:
            return self.get_all()
        if state:
            self.watched[clean_key] = time.time()
        else:
            self.watched.pop(clean_key, None)
        self._save()
        return self.get_all()


watched_manager = WatchedManager()

