"""Pool of Gemini API keys persisted to disk.

Single source of truth for which keys the bot can use. Keys live in
``GEMINI_KEYS_FILE`` (default ``gemini_keys.json``, gitignored) and carry the
Discord user_id of whoever donated them so we can credit them later.

``load_from_disk()`` runs at startup. ``add_key()`` is called from DM handlers
when someone sends a fresh key to the bot or userbot — it persists the JSON
and hot-adds the key to the active pool without restart.

If the JSON file doesn't exist on first run, we seed it from
``config.GEMINI_API_KEYS`` (or the legacy single ``GEMINI_API_KEY``) so legacy
deployments keep working.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Optional

import config

logger = logging.getLogger("bot.gemini.keys")

# Discord-only formats:
#   AIzaSy... → 39 chars total (classic Google API key)
#   AQ.Ab8RN6... → ephemeral OAuth-derived keys (~50 chars)
# Captura el token contiguo despues del prefijo conocido.
_GEMINI_KEY_RE = re.compile(
    r"\b(?:AIza[\w-]{20,80}|AQ\.[A-Za-z0-9_\-]{20,120})"
)

_keys: list[dict] = []  # cada item: {"key", "owner_name", "owner_id", "note", "source"}
_lock = asyncio.Lock()


def extract_keys_from_text(text: str) -> list[str]:
    """Pull every Gemini-shaped key from a free-form string. Order preserved,
    duplicates collapsed."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _GEMINI_KEY_RE.finditer(text):
        k = m.group(0)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def active_keys() -> list[str]:
    """Return the raw key strings currently in the pool, in donation order."""
    return [item["key"] for item in _keys if item.get("key")]


def list_entries() -> list[dict]:
    """Return the full registry (key + owner metadata) for diagnostics."""
    return list(_keys)


def has_user_key(user_id) -> bool:
    """True iff this Discord user_id has at least one key in the pool.

    ``owner_id`` is stored as a string. Empty owner_ids (e.g. the ``.env``
    bootstrap source) never match a real Discord id.
    """
    if user_id is None:
        return False
    target = str(user_id)
    if not target:
        return False
    return any((item.get("owner_id") or "") == target for item in _keys)


def format_contributors_line() -> str:
    """Render the deduped list of donors backing the current pool.

    Counts keys per ``owner_name`` so credit is proportional to donations.
    Owners labeled ``unknown`` (e.g. ``.env`` bootstrap) are skipped.
    Returns ``""`` when there is nothing meaningful to show.
    """
    counts: dict[str, int] = {}
    for entry in _keys:
        name = (entry.get("owner_name") or "").strip()
        if not name or name.lower() == "unknown":
            continue
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    parts = [
        f"{name} ({n})" if n > 1 else name
        for name, n in counts.items()
    ]
    return f"🙏 Contribuyentes actuales: {', '.join(parts)}."


def load_from_disk(path: Optional[str] = None) -> int:
    """Load the key registry from ``path`` (defaults to ``GEMINI_KEYS_FILE``).

    If the file is missing or empty, seeds the registry from
    ``config.GEMINI_API_KEYS`` so an .env-only deployment keeps working.
    Returns the number of keys now in the pool.
    """
    target = path or config.GEMINI_KEYS_FILE
    loaded: list[dict] = []
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in (data.get("keys") or []):
            if not isinstance(item, dict):
                continue
            key = (item.get("key") or "").strip()
            if not key:
                continue
            loaded.append({
                "key": key,
                "owner_name": str(item.get("owner_name") or "unknown"),
                "owner_id": str(item.get("owner_id") or ""),
                "note": str(item.get("note") or ""),
                "source": str(item.get("source") or "manual"),
            })
    except FileNotFoundError:
        logger.info("gemini keys file %s not found — bootstrapping from env", target)
    except Exception:
        logger.exception("gemini keys file %s unreadable", target)

    if not loaded:
        for k in config.GEMINI_API_KEYS:
            loaded.append({
                "key": k,
                "owner_name": "unknown",
                "owner_id": "",
                "note": "bootstrap from .env",
                "source": "env",
            })

    _keys.clear()
    _keys.extend(loaded)
    logger.info("gemini keys: loaded %d entries from %s", len(_keys), target)
    return len(_keys)


def _persist_sync(path: str) -> None:
    """Atomic write of the current registry. Runs in a thread."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "_comment": (
            "Mapeo de API keys de Gemini a sus duenos (Discord user_id). "
            "El bot lo lee al startup y lo edita cuando alguien manda una key "
            "nueva por DM. Mantener fuera de git."
        ),
        "keys": _keys,
    }
    fd, tmp = tempfile.mkstemp(prefix=".gemini_keys_", suffix=".json", dir=directory)
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
    """Hot-add a key to the pool and persist the registry.

    Returns ``(True, "added")`` on success, ``(False, reason)`` if the key
    looks invalid or is already in the pool.
    """
    key = (key or "").strip()
    if not key:
        return False, "empty key"
    if not _GEMINI_KEY_RE.fullmatch(key):
        return False, "key shape does not match AIza... or AQ.Ab8...."
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
        try:
            await asyncio.to_thread(_persist_sync, config.GEMINI_KEYS_FILE)
        except Exception:
            logger.exception("gemini keys: persist failed")
            _keys.pop()  # rollback in-memory si falla el disco
            return False, "persist failed"
    logger.info(
        "gemini keys: added one (owner=%s/%s, source=%s, total=%d)",
        owner_name, owner_id, source, len(_keys),
    )
    return True, "added"
