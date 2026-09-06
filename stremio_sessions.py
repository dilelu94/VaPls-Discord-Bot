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
    def __init__(self, default_ttl_hours: float = 10 / 60.0, storage_path: str = _STORAGE_PATH):
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

    def get_session(self, token: str) -> Optional[StremioSession]:
        if not token:
            return None
        sess = self.sessions.get(token)
        if not sess:
            return None
        if time.time() > sess.expires_at:
            logger.info("Stremio session expired: token=%s", token)
            self.sessions.pop(token, None)
            self._save_sessions()
            return None
        return sess

    def validate_token(self, token: str) -> bool:
        return self.get_session(token) is not None


session_manager = StremioSessionManager()
