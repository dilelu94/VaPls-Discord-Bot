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

    # Case 1: Pure 40-char infohash
    if is_infohash(raw):
        hash_val = raw.lower()
        magnet = f"magnet:?xt=urn:btih:{hash_val}"
        return magnet, f"Torrent ({hash_val[:8]})"

    # Case 2: Stremio resolve URL containing 40-char infohash
    m = STREMIO_RESOLVE_RE.search(raw)
    if m:
        hash_val = m.group(1).lower()
        # Extract title if present in path
        title = "Torrent Stream"
        path_parts = raw.split("/")
        for part in reversed(path_parts):
            unquoted = urllib.parse.unquote(part)
            if unquoted.endswith((".mkv", ".mp4", ".avi", ".mov")):
                title = unquoted
                break
        magnet = f"magnet:?xt=urn:btih:{hash_val}"
        return magnet, title

    # Case 3: Standard magnet link
    if is_magnet_link(raw):
        # Extract dn (display name) if present
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        dn = parsed.get("dn", ["Magnet Stream"])[0]
        return raw, dn

    # Case 4: Prefix 'torrent:'
    if raw.lower().startswith("torrent:"):
        clean = raw[8:].strip()
        if is_magnet_link(clean) or is_infohash(clean):
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
            torrentio_url = f"https://torrentio.strem.fun/stream/movie/{imdb_id}.json"
            req = urllib.request.Request(torrentio_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                tdata = json.loads(resp.read().decode())
                streams = tdata.get("streams", [])
                for s in streams:
                    infohash = s.get("infoHash", "")
                    if not infohash:
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
                    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(main_title)}"

                    results.append(
                        TorrentStreamItem(
                            title=main_title,
                            quality=quality,
                            seeders=seeders,
                            size=size_str,
                            magnet_or_url=magnet,
                            infohash=infohash,
                        )
                    )
                    if len(results) >= limit:
                        break
        except Exception as e:
            log.warning("Torrentio fetch failed for %s: %s", imdb_id, e)

    return results
