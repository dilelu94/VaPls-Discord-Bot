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
IPTV_SPA_URL = "https://iptv-org.github.io/iptv/languages/spa.m3u"
IPTV_ENG_URL = "https://iptv-org.github.io/iptv/languages/eng.m3u"

CACHE_PATH = "data/iptv_cache.m3u"
CACHE_SPA_PATH = "data/iptv_spa_cache.m3u"
CACHE_ENG_PATH = "data/iptv_eng_cache.m3u"
CACHE_MAX_AGE = 6 * 3600


class Channel:
    name: str
    url: str
    tvg_id: str
    tvg_logo: str
    group: str
    language: str

    def __init__(self) -> None:
        self.name = ""
        self.url = ""
        self.tvg_id = ""
        self.tvg_logo = ""
        self.group = ""
        self.language = "other"


_cached: list[Channel] = []
_cache_ts: float = 0


def _parse_m3u(text: str) -> list[Channel]:
    entries: list[Channel] = []
    if not text:
        return entries
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


async def _fetch_and_cache(session: aiohttp.ClientSession, url: str, path: str) -> str:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            text = await resp.text()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return text
    except Exception as e:
        logger.warning("failed to fetch and cache %s: %s", url, e)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e2:
                logger.warning("failed to read cache path %s: %s", path, e2)
        return ""


async def _ensure_cache(session: aiohttp.ClientSession) -> list[Channel]:
    global _cached, _cache_ts
    now = time.time()
    if _cached and (now - _cache_ts) < CACHE_MAX_AGE:
        return _cached

    # Fetch main, Spanish and English playlists concurrently
    tasks = [
        _fetch_and_cache(session, IPTV_M3U_URL, CACHE_PATH),
        _fetch_and_cache(session, IPTV_SPA_URL, CACHE_SPA_PATH),
        _fetch_and_cache(session, IPTV_ENG_URL, CACHE_ENG_PATH),
    ]
    main_text, spa_text, eng_text = await asyncio.gather(*tasks)

    if not main_text:
        if _cached:
            return _cached
        # If cache is not in-memory but exists on disk, load it
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    main_text = f.read()
            except Exception as e:
                logger.warning("failed to read main cache from disk: %s", e)
        if not main_text:
            return []

    # Parse language playlists to build lookup sets
    spa_channels = _parse_m3u(spa_text)
    spa_ids = {ch.tvg_id for ch in spa_channels if ch.tvg_id}
    spa_urls = {ch.url for ch in spa_channels if ch.url}

    eng_channels = _parse_m3u(eng_text)
    eng_ids = {ch.tvg_id for ch in eng_channels if ch.tvg_id}
    eng_urls = {ch.url for ch in eng_channels if ch.url}

    # Parse main playlist
    entries = _parse_m3u(main_text)

    # Classify language
    for ch in entries:
        if (ch.tvg_id and ch.tvg_id in spa_ids) or ch.url in spa_urls:
            ch.language = "es"
        elif (ch.tvg_id and ch.tvg_id in eng_ids) or ch.url in eng_urls:
            ch.language = "en"
        else:
            ch.language = "other"

    _cached = entries
    _cache_ts = time.time()
    logger.info("iptv: loaded %d channels from playlist", len(_cached))
    return _cached


async def get_all_channels() -> list[Channel]:
    """Get all loaded channels, loading them if cache is empty or expired."""
    async with aiohttp.ClientSession() as session:
        return await _ensure_cache(session)


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
