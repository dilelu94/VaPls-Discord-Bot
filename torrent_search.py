"""Stremio-style torrent search and magnet link resolution module.

Provides utilities to identify magnet links, infohashes, and Stremio/Torrentio
resolve URLs, as well as searching for movie/series streams via Stremio-compatible APIs.
"""

from dataclasses import dataclass
import json
import logging
import re
from typing import Optional
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

import config

# Regular expressions for magnet links, infohashes, and Stremio resolve URLs
HEX_INFOHASH_RE = re.compile(r"\b[a-fA-F0-9]{40}\b")
B32_INFOHASH_RE = re.compile(r"\b[a-zA-Z2-7]{32}\b")
STREMIO_RESOLVE_RE = re.compile(
    r"https?://[^/]+/resolve/[^/]+/[^/]+/([a-fA-F0-9]{40})", re.IGNORECASE
)


@dataclass
class TorrentStreamItem:
    """Represents a torrent stream result for UI selection."""

    title: str
    quality: str
    seeders: int
    size: str
    magnet_or_url: str
    infohash: str

    def format_display_label(self) -> str:
        """Returns a string suitable for Discord SelectMenu options (max 100 chars)."""
        seeders_str = f"👤 {self.seeders}" if self.seeders >= 0 else ""
        size_str = f"💾 {self.size}" if self.size else ""
        qual_str = f"[{self.quality}]" if self.quality else ""

        meta_parts = [p for p in (qual_str, seeders_str, size_str) if p]
        meta = " | ".join(meta_parts)
        if meta:
            full = f"{self.title[:65]} ({meta})"
        else:
            full = self.title
        return full[:100]


def is_magnet_link(text: str) -> bool:
    """Check if the given text is a magnet link."""
    if not text:
        return False
    raw = text.strip().lower()
    return raw.startswith("magnet:?") or "xt=urn:btih:" in raw


def is_infohash(text: str) -> bool:
    """Check if the text is a standalone 40-char hex or 32-char base32 infohash."""
    if not text:
        return False
    raw = text.strip()
    return bool(HEX_INFOHASH_RE.fullmatch(raw) or B32_INFOHASH_RE.fullmatch(raw))


def is_stremio_resolve_url(text: str) -> bool:
    """Check if the text is a Stremio/Torrentio resolve URL."""
    if not text:
        return False
    raw = text.strip()
    return bool(STREMIO_RESOLVE_RE.search(raw) or "torrentio.strem.fun/resolve/" in raw)


def is_torrent_input(text: str) -> bool:
    """Check if text is a magnet link, infohash, .torrent URL, or Stremio resolve URL."""
    if not text:
        return False
    raw = text.strip()
    if is_magnet_link(raw) or is_infohash(raw) or is_stremio_resolve_url(raw):
        return True
    if raw.lower().endswith(".torrent") or raw.lower().startswith("torrent:"):
        return True
    return False


def resolve_stremio_or_magnet_url(text: str) -> tuple[str, Optional[str]]:
    """Resolves an input string to a standardized magnet URI or direct URL.

    Returns:
        (resolved_url_or_magnet, channel_name_or_title)
    """
    raw = text.strip()

    # Case 1: Direct TorBox / Stremio resolve URL -> keep URL for direct HTTP streaming!
    if is_stremio_resolve_url(raw):
        title = "Torrent Stream"
        path_parts = raw.split("/")
        for part in reversed(path_parts):
            unquoted = urllib.parse.unquote(part)
            if unquoted.endswith((".mkv", ".mp4", ".avi", ".mov")):
                title = unquoted
                break
        return raw, title

    # Case 2: Pure 40-char infohash
    if is_infohash(raw):
        hash_val = raw.lower()
        magnet = f"magnet:?xt=urn:btih:{hash_val}"
        return magnet, f"Torrent ({hash_val[:8]})"

    # Case 3: Standard magnet link
    if is_magnet_link(raw):
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        dn = parsed.get("dn", ["Magnet Stream"])[0]
        return raw, dn

    # Case 4: Prefix 'torrent:'
    if raw.lower().startswith("torrent:"):
        clean = raw[8:].strip()
        if is_magnet_link(clean) or is_infohash(clean) or is_stremio_resolve_url(clean):
            return resolve_stremio_or_magnet_url(clean)

    return raw, "Stream Directo"


def parse_torrent_quality(title: str) -> str:
    """Extract resolution/quality string from title (e.g. 1080p, 720p, 4K)."""
    title_upper = title.upper()
    if "2160P" in title_upper or "4K" in title_upper:
        return "4K"
    if "1080P" in title_upper:
        return "1080p"
    if "720P" in title_upper:
        return "720p"
    if "480P" in title_upper:
        return "480p"
    return "HD"


async def search_stremio_torrents(query: str, limit: int = 10) -> list[TorrentStreamItem]:
    """Search for movie/series torrent streams via Stremio Cinemeta and Torrentio APIs.

    Args:
        query: Movie or show name (e.g. 'The Matrix', 'Inception').
        limit: Max number of results.

    Returns:
        List of TorrentStreamItem.
    """
    clean_query = query.strip()
    if clean_query.lower().startswith("torrent:"):
        clean_query = clean_query[8:].strip()

    if not clean_query:
        return []

    results: list[TorrentStreamItem] = []

    # 1. Resolve query to IMDb ID using Cinemeta
    imdb_id = None
    try:
        search_url = f"https://v3-cinemeta.strem.fun/catalog/movie/top/search={urllib.parse.quote(clean_query)}.json"
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode())
            metas = data.get("metas", [])
            if metas:
                imdb_id = metas[0].get("id")
    except Exception as e:
        log.warning("Cinemeta search failed for '%s': %s", clean_query, e)

    # 2. Fetch streams from Torrentio if IMDb ID was found
    if imdb_id:
        try:
            debrid_config = getattr(config, "TORRENTIO_CONFIG", "torbox=90f73123-7565-4ae3-b672-aa96bc026c50")
            prefix = f"{debrid_config}/" if debrid_config else ""
            torrentio_url = f"https://torrentio.strem.fun/{prefix}stream/movie/{imdb_id}.json"
            req = urllib.request.Request(torrentio_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                tdata = json.loads(resp.read().decode())
                streams = tdata.get("streams", [])
                for s in streams:
                    direct_url = s.get("url", "")
                    infohash = s.get("infoHash", "")
                    if not direct_url and not infohash:
                        continue

                    title_raw = s.get("title", s.get("name", "Torrent Stream"))
                    lines = [line.strip() for line in title_raw.split("\n") if line.strip()]
                    main_title = lines[0] if lines else "Torrent Stream"

                    # Parse seeders & size if present in title string
                    seeders = -1
                    size_str = ""
                    for line in lines[1:]:
                        if "👤" in line or "seeders" in line.lower():
                            m_s = re.search(r"(\d+)", line)
                            if m_s:
                                seeders = int(m_s.group(1))
                        if "💾" in line or "GB" in line or "MB" in line:
                            size_str = line.replace("💾", "").strip()

                    quality = parse_torrent_quality(main_title + " " + s.get("name", ""))
                    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(main_title)}" if infohash else ""

                    results.append(
                        TorrentStreamItem(
                            title=main_title,
                            quality=quality,
                            seeders=seeders,
                            size=size_str,
                            magnet_or_url=direct_url or magnet,
                            infohash=infohash or "torbox",
                        )
                    )
                    if len(results) >= limit:
                        break
        except Exception as e:
            log.warning("Torrentio fetch failed for %s: %s", imdb_id, e)

    return results


def search_stremio_catalog_sync(query: str, type_filter: str = "all") -> list[dict]:
    """Search Cinemeta and Kitsu catalogs synchronously."""
    clean_query = query.strip()
    if not clean_query:
        return []

    results = []
    seen_ids = set()
    encoded = urllib.parse.quote(clean_query)

    endpoints = []
    if type_filter in ("all", "movie"):
        endpoints.append(("movie", f"https://v3-cinemeta.strem.fun/catalog/movie/top/search={encoded}.json"))
    if type_filter in ("all", "series"):
        endpoints.append(("series", f"https://v3-cinemeta.strem.fun/catalog/series/top/search={encoded}.json"))
    if type_filter in ("all", "anime"):
        endpoints.append(("anime", f"https://anime-kitsu.strem.fun/catalog/anime/kitsu-anime-list/search={encoded}.json"))

    for cat_type, url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode())
                metas = data.get("metas", [])
                for item in metas:
                    item_id = item.get("id")
                    if not item_id or item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    poster = item.get("poster") or ""
                    banner = item.get("background") or ""
                    year = str(item.get("year", "")) if item.get("year") else ""
                    description = item.get("description") or ""

                    item_type = cat_type
                    if item_id.startswith("kitsu:"):
                        item_type = "anime"

                    results.append({
                        "id": item_id,
                        "type": item_type,
                        "title": item.get("name", "Desconocido"),
                        "poster": poster,
                        "banner": banner,
                        "year": year,
                        "description": description,
                        "imdb_id": item.get("imdb_id") or (item_id if item_id.startswith("tt") else None),
                    })
        except Exception as e:
            log.warning("Catalog search error for %s (%s): %s", url, cat_type, e)

    return results


async def search_stremio_catalog(query: str, type_filter: str = "all") -> list[dict]:
    """Async wrapper for search_stremio_catalog_sync."""
    import asyncio
    return await asyncio.to_thread(search_stremio_catalog_sync, query, type_filter)


def get_stremio_meta_sync(item_type: str, item_id: str) -> dict:
    """Fetch metadata and episode list for a movie, series, or anime."""
    url = ""
    if item_id.startswith("kitsu:") or item_type == "anime":
        url = f"https://anime-kitsu.strem.fun/meta/anime/{item_id}.json"
    else:
        url = f"https://v3-cinemeta.strem.fun/meta/{item_type}/{item_id}.json"

    meta_data = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode())
            meta_data = data.get("meta", {})
    except Exception as e:
        log.warning("Fetch meta failed for %s (%s): %s", item_id, item_type, e)

    episodes = []
    raw_videos = meta_data.get("videos", [])
    for v in raw_videos:
        ep_season = v.get("season") or v.get("imdbSeason") or 1
        ep_num = v.get("episode") or v.get("imdbEpisode") or 1
        ep_title = v.get("title") or f"Episodio {ep_num}"
        episodes.append({
            "id": v.get("id") or f"{item_id}:{ep_season}:{ep_num}",
            "title": ep_title,
            "season": ep_season,
            "episode": ep_num,
            "thumbnail": v.get("thumbnail") or v.get("poster") or meta_data.get("poster") or "",
            "overview": v.get("overview") or v.get("description") or "",
            "released": v.get("released") or "",
        })

    return {
        "id": item_id,
        "imdb_id": meta_data.get("imdb_id") or (item_id if item_id.startswith("tt") else None),
        "type": item_type,
        "title": meta_data.get("name", "Desconocido"),
        "poster": meta_data.get("poster") or "",
        "banner": meta_data.get("background") or "",
        "description": meta_data.get("description") or "",
        "year": str(meta_data.get("year", "")),
        "genres": meta_data.get("genres", []),
        "episodes": episodes,
    }


async def get_stremio_meta(item_type: str, item_id: str) -> dict:
    """Async wrapper for get_stremio_meta_sync."""
    import asyncio
    return await asyncio.to_thread(get_stremio_meta_sync, item_type, item_id)


def get_stremio_streams_sync(
    item_type: str,
    item_id: str,
    season: int = 1,
    episode: int = 1,
    imdb_id: Optional[str] = None
) -> list[dict]:
    """Fetch TorBox / Torrentio streams for movie, series, or anime."""
    debrid_config = getattr(config, "TORRENTIO_CONFIG", "torbox=90f73123-7565-4ae3-b672-aa96bc026c50")
    prefix = f"{debrid_config}/" if debrid_config else ""

    target_imdb = imdb_id
    if not target_imdb and item_id.startswith("tt"):
        target_imdb = item_id

    if not target_imdb and item_id.startswith("kitsu:"):
        meta = get_stremio_meta_sync(item_type, item_id)
        target_imdb = meta.get("imdb_id")

    urls_to_try = []
    if target_imdb:
        if item_type == "movie":
            urls_to_try.append(f"https://torrentio.strem.fun/{prefix}stream/movie/{target_imdb}.json")
        else:
            urls_to_try.append(f"https://torrentio.strem.fun/{prefix}stream/series/{target_imdb}:{season}:{episode}.json")

    if item_id.startswith("kitsu:"):
        urls_to_try.append(f"https://torrentio.strem.fun/{prefix}stream/series/{item_id}:{episode}.json")

    streams_out = []
    seen_urls = set()

    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                tdata = json.loads(resp.read().decode())
                raw_streams = tdata.get("streams", [])
                for s in raw_streams:
                    direct_url = s.get("url", "")
                    infohash = s.get("infoHash", "")
                    if not direct_url and not infohash:
                        continue
                    if direct_url and direct_url in seen_urls:
                        continue
                    if direct_url:
                        seen_urls.add(direct_url)

                    title_raw = s.get("title", s.get("name", "Torrent Stream"))
                    lines = [line.strip() for line in title_raw.split("\n") if line.strip()]
                    main_title = lines[0] if lines else "Torrent Stream"

                    seeders = -1
                    size_str = ""
                    details_str = ""
                    for line in lines[1:]:
                        if "👤" in line or "seeders" in line.lower():
                            m_s = re.search(r"(\d+)", line)
                            if m_s:
                                seeders = int(m_s.group(1))
                        if "💾" in line or "GB" in line or "MB" in line:
                            size_str = line.replace("💾", "").strip()
                        if "⚙️" in line or "🔊" in line or "🌐" in line:
                            details_str += " " + line

                    quality = parse_torrent_quality(main_title + " " + s.get("name", ""))
                    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(main_title)}" if infohash else ""

                    is_torbox_direct = "torrentio.strem.fun/resolve/torbox/" in direct_url or "tb-cdn" in direct_url

                    streams_out.append({
                        "name": s.get("name", "Torrentio"),
                        "title": main_title,
                        "quality": quality,
                        "seeders": seeders,
                        "size": size_str,
                        "details": details_str.strip(),
                        "url": direct_url or magnet,
                        "infohash": infohash or "torbox",
                        "is_direct": is_torbox_direct,
                    })
        except Exception as e:
            log.warning("Error fetching streams from %s: %s", url, e)

    return streams_out


async def get_stremio_streams(
    item_type: str,
    item_id: str,
    season: int = 1,
    episode: int = 1,
    imdb_id: Optional[str] = None
) -> list[dict]:
    """Async wrapper for get_stremio_streams_sync."""
    import asyncio
    return await asyncio.to_thread(get_stremio_streams_sync, item_type, item_id, season, episode, imdb_id)

