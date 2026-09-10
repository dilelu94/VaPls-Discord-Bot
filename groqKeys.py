"""Pool of Groq API keys persisted to disk.

Single source of truth for Groq API keys used by the bot and userbot. Keys live in
``GROQ_KEYS_FILE`` (default ``data/groq_keys.json``, gitignored) and carry the
Discord user_id of whoever donated them so we can credit them later.

``load_from_disk()`` runs at startup. ``add_key()`` is called from DM handlers
when someone sends a fresh key (starting with ``gsk_...``) to the bot or userbot —
it persists the JSON and hot-adds the key to the active pool without restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import sys
import time
from typing import Optional

import config

logger = logging.getLogger("bot.groq.keys")

# Groq API keys match: gsk_... (30 to 100 alphanumeric/hyphen/underscore chars)
_GROQ_KEY_RE = re.compile(r"\bgsk_[\w-]{30,100}\b")

_keys: list[dict] = []  # cada item: {"key", "owner_name", "owner_id", "note", "source"}
_lock = asyncio.Lock()
_next_key_idx: int = 0
_key_cooldowns: dict[str, float] = {}  # key -> timestamp when available again


def extract_keys_from_text(text: str) -> list[str]:
    """Pull every Groq-shaped key (gsk_...) from a free-form string."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _GROQ_KEY_RE.finditer(text):
        k = m.group(0)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def active_keys() -> list[str]:
    """Return raw key strings currently in the pool."""
    return [item["key"] for item in _keys if item.get("key")]


def get_next_groq_key() -> str:
    """Return the next Groq API key in the pool using round-robin rotation, skipping keys in cooldown."""
    global _next_key_idx
    keys = active_keys()
    if not keys:
        return getattr(config, "GROQ_API_KEY", "")
    now = time.time()
    available = [k for k in keys if _key_cooldowns.get(k, 0.0) <= now]
    if not available:
        return min(keys, key=lambda k: _key_cooldowns.get(k, 0.0))
    start = _next_key_idx % len(keys)
    for offset in range(len(keys)):
        candidate = keys[(start + offset) % len(keys)]
        if candidate in available:
            _next_key_idx = (start + offset + 1) % len(keys)
            return candidate
    return keys[0]


def mark_key_cooldown(key: str, seconds: float = 60.0) -> None:
    """Put a key into temporary cooldown (e.g. for rate limit HTTP 429)."""
    if key:
        _key_cooldowns[key] = time.time() + seconds


def mark_key_dead(key: str) -> None:
    """Put an invalid/expired key into 24h cooldown (HTTP 401/403)."""
    if key:
        _key_cooldowns[key] = time.time() + 86400.0


def list_entries() -> list[dict]:
    """Return full registry for diagnostics."""
    return list(_keys)


def has_user_key(user_id) -> bool:
    """True iff this Discord user_id has at least one Groq key in the pool."""
    if user_id is None:
        return False
    target = str(user_id)
    if not target:
        return False
    return any((item.get("owner_id") or "") == target for item in _keys)


def format_contributors_line() -> str:
    """Render donor credits."""
    counts: dict[str, int] = {}
    for entry in _keys:
        name = (entry.get("owner_name") or "").strip()
        if not name or name.lower() == "unknown":
            continue
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    parts = [f"{name} ({n})" if n > 1 else name for name, n in counts.items()]
    return f"🙏 Contribuyentes de Groq API: {', '.join(parts)}."


def _update_global_groq_key(new_key: str) -> None:
    """Hot-update config.GROQ_API_KEY in root config and userbot config."""
    if new_key:
        config.GROQ_API_KEY = new_key
        # Update userbot config if loaded
        userbot_mod = sys.modules.get("userbot.bot") or sys.modules.get("bot")
        if userbot_mod and hasattr(userbot_mod, "config"):
            userbot_mod.config.GROQ_API_KEY = new_key
            if hasattr(userbot_mod.config, "STT_PROVIDER") and userbot_mod.config.STT_PROVIDER == "local":
                userbot_mod.config.STT_PROVIDER = "groq"


def load_from_disk(path: Optional[str] = None) -> int:
    """Load key registry from disk or bootstrap from env."""
    target = path or getattr(config, "GROQ_KEYS_FILE", "data/groq_keys.json")
    loaded: list[dict] = []
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("keys") or []:
            if not isinstance(item, dict):
                continue
            key = (item.get("key") or "").strip()
            if not key:
                continue
            loaded.append(
                {
                    "key": key,
                    "owner_name": str(item.get("owner_name") or "unknown"),
                    "owner_id": str(item.get("owner_id") or ""),
                    "note": str(item.get("note") or ""),
                    "source": str(item.get("source") or "manual"),
                }
            )
    except FileNotFoundError:
        logger.info("groq keys file %s not found — bootstrapping from env", target)
    except Exception:
        logger.exception("groq keys file %s unreadable", target)

    if not loaded:
        env_key = getattr(config, "GROQ_API_KEY", "")
        if env_key:
            loaded.append(
                {
                    "key": env_key,
                    "owner_name": "unknown",
                    "owner_id": "",
                    "note": "bootstrap from .env",
                    "source": "env",
                }
            )

    _keys.clear()
    _keys.extend(loaded)
    if _keys:
        _update_global_groq_key(_keys[0]["key"])
    logger.info("groq keys: loaded %d entries from %s", len(_keys), target)
    return len(_keys)


def _persist_sync(path: str) -> None:
    """Atomic write of the current registry."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "_comment": (
            "Mapeo de Groq API keys a sus duenos (Discord user_id). "
            "El bot lo lee al startup y lo edita cuando alguien manda una key "
            "nueva por DM. Mantener fuera de git."
        ),
        "keys": _keys,
    }
    fd, tmp = tempfile.mkstemp(prefix=".groq_keys_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def add_key(
    key: str,
    *,
    owner_id: str,
    owner_name: str,
    source: str,
    note: str = "",
) -> tuple[bool, str]:
    """Hot-add a Groq API key to the pool and persist."""
    key = (key or "").strip()
    if not key:
        return False, "empty key"
    if not _GROQ_KEY_RE.fullmatch(key):
        return False, "key shape does not match gsk_..."
    async with _lock:
        if any(item.get("key") == key for item in _keys):
            return False, "already in pool"
        entry = {
            "key": key,
            "owner_name": str(owner_name or "unknown"),
            "owner_id": str(owner_id or ""),
            "note": note,
            "source": source,
        }
        _keys.append(entry)
        target = getattr(config, "GROQ_KEYS_FILE", "data/groq_keys.json")
        try:
            await asyncio.to_thread(_persist_sync, target)
        except Exception:
            logger.exception("groq keys: persist failed")
            _keys.pop()
            return False, "persist failed"
        _update_global_groq_key(key)
    logger.info(
        "groq keys: added one (owner=%s/%s, source=%s, total=%d)",
        owner_name,
        owner_id,
        source,
        len(_keys),
    )
    return True, "added"
