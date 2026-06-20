"""IPTV channel search using iptv-org's public M3U playlist."""

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urljoin
import aiohttp

logger = logging.getLogger("iptv")

IPTV_M3U_URL = "https://iptv-org.github.io/iptv/index.m3u"
CACHE_PATH = "data/iptv_cache.m3u"
CACHE_MAX_AGE = 6 * 3600


class Channel:
    name: str
    url: str
    tvg_id: str
    tvg_logo: str
    group: str

    def __init__(self) -> None:
        self.name = ""
        self.url = ""
        self.tvg_id = ""
        self.tvg_logo = ""
        self.group = ""


_cached: list[Channel] = []
_cache_ts: float = 0


def _parse_m3u(text: str) -> list[Channel]:
    entries: list[Channel] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("#EXTINF:"):
            i += 1
            continue
        ch = Channel()
        m = re.search(r'tvg-id="([^"]*)"', line)
        if m:
            ch.tvg_id = m.group(1)
        m = re.search(r'tvg-name="([^"]*)"', line)
        if m:
            ch.name = m.group(1)
        m = re.search(r'tvg-logo="([^"]*)"', line)
        if m:
            ch.tvg_logo = m.group(1)
        m = re.search(r'group-title="([^"]*)"', line)
        if m:
            ch.group = m.group(1)
        if not ch.name:
            idx = line.rfind(",")
            if idx >= 0:
                ch.name = line[idx + 1 :].strip()
        i += 1
        if i < len(lines):
            url = lines[i].strip()
            if url and not url.startswith("#"):
                ch.url = url
                entries.append(ch)
        i += 1
    return entries


async def _ensure_cache(session: aiohttp.ClientSession) -> list[Channel]:
    global _cached, _cache_ts
    now = time.time()
    if _cached and (now - _cache_ts) < CACHE_MAX_AGE:
        return _cached
    try:
        async with session.get(
            IPTV_M3U_URL, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            text = await resp.text()
    except Exception as e:
        logger.warning("failed to fetch iptv playlist: %s", e)
        if _cached:
            return _cached
        return []
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logger.warning("failed to cache iptv playlist: %s", e)
    _cached = _parse_m3u(text)
    _cache_ts = time.time()
    logger.info("iptv: loaded %d channels from playlist", len(_cached))
    return _cached


async def search(query: str, limit: int = 5) -> list[Channel]:
    """Search IPTV channels by name. Returns up to ``limit`` matches."""
    async with aiohttp.ClientSession() as session:
        entries = await _ensure_cache(session)
    if not entries:
        return []
    query_lower = query.lower()
    scored: list[tuple[int, Channel]] = []
    for ch in entries:
        name_lower = ch.name.lower()
        if query_lower in name_lower:
            # Prefer exact substring matches and shorter names
            score = len(query) / max(len(ch.name), 1) * 100
            if name_lower.startswith(query_lower):
                score += 50
            scored.append((score, ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [ch for _, ch in scored[:limit]]


async def get_best(query: str) -> Optional[Channel]:
    """Return the single best match for a query, or None."""
    results = await search(query, limit=1)
    return results[0] if results else None
