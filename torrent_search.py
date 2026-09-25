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

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


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


def resolve_torbox_url(url: str) -> Optional[str]:
    """Resolves a torrentio/torbox resolve URL directly using TorBox official API."""
    if "resolve/torbox/" not in url:
        return None
    try:
        parts = url.split("resolve/torbox/")[1].split("/")
        token = parts[0]
        infohash = parts[1].lower()
        return resolve_torbox_hash(token, infohash)
    except Exception as e:
        log.warning("[TORBOX RESOLVE API] Error parsing %s: %s", url, e)
    return None


def resolve_torbox_hash(token: str, infohash: str) -> Optional[str]:
    """Resolves an infohash to a direct TorBox CDN stream URL using TorBox official API."""
    if not token or not infohash:
        return None
    infohash = infohash.lower().strip()
    headers = {"Authorization": f"Bearer {token}", "User-Agent": BROWSER_HEADERS["User-Agent"]}

    # 1. Try checking existing torrents in user's library
    try:
        req = urllib.request.Request(
            f"https://api.torbox.app/v1/api/torrents/mylist?hash={infohash}",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode())
            torrents = data.get("data", [])
            matched = next((t for t in torrents if str(t.get("hash", "")).lower() == infohash), None)
            if matched and matched.get("id"):
                torrent_id = matched["id"]
                req_dl = urllib.request.Request(
                    f"https://api.torbox.app/v1/api/torrents/requestdl?token={token}&torrent_id={torrent_id}",
                    headers=headers,
                )
                with urllib.request.urlopen(req_dl, timeout=5.0) as rdl:
                    link = json.loads(rdl.read().decode()).get("data")
                    if link and link.startswith("http"):
                        log.info("[TORBOX API] Found in mylist: %s -> %s", infohash[:8], link[:60])
                        return link
    except Exception as e:
        log.debug("[TORBOX API] MyList check error for %s: %s", infohash[:8], e)

    # 2. If not in mylist, create/request cached torrent on TorBox
    try:
        magnet = f"magnet:?xt=urn:btih:{infohash}"
        post_data = urllib.parse.urlencode({"magnet": magnet, "seed": 1}).encode()
        post_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        req_create = urllib.request.Request(
            "https://api.torbox.app/v1/api/torrents/createtorrent",
            data=post_data,
            headers=post_headers,
        )
        with urllib.request.urlopen(req_create, timeout=8.0) as rc:
            cdata = json.loads(rc.read().decode())
            c_info = cdata.get("data") or {}
            torrent_id = c_info.get("torrent_id") or c_info.get("id")
            if torrent_id:
                req_dl = urllib.request.Request(
                    f"https://api.torbox.app/v1/api/torrents/requestdl?token={token}&torrent_id={torrent_id}",
                    headers=headers,
                )
                with urllib.request.urlopen(req_dl, timeout=5.0) as rdl:
                    link = json.loads(rdl.read().decode()).get("data")
                    if link and link.startswith("http"):
                        log.info("[TORBOX API] Created/cached torrent: %s -> %s", infohash[:8], link[:60])
                        return link
    except Exception as e:
        log.warning("[TORBOX API] Create torrent error for %s: %s", infohash[:8], e)

    return None


def resolve_redirect_url(url: str) -> str:
    """Follow HTTP 302 redirects to find the direct CDN stream URL."""
    if not url or not url.startswith(("http://", "https://")):
        return url
    if "resolve/torbox/" in url:
        tb_res = resolve_torbox_url(url)
        if tb_res:
            return tb_res
    try:
        req = urllib.request.Request(
            url,
            headers=BROWSER_HEADERS,
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            final_url = resp.geturl()
            if final_url and final_url != url:
                log.info("[STREAM RESOLVE] Followed redirect: %s -> %s", url, final_url)
                return final_url
    except Exception as e:
        log.warning("[STREAM RESOLVE] Redirect lookup for %s failed: %s", url, e)
    return url


import base64


def extract_infohash(text: str) -> Optional[str]:
    """Extract 40-character hex infohash from magnet link, base32 infohash, or infohash string."""
    if not text:
        return None
    raw = text.strip()
    m40 = re.search(r"urn:btih:([a-fA-F0-9]{40})", raw, re.IGNORECASE)
    if m40:
        return m40.group(1).lower()
    m32 = re.search(r"urn:btih:([a-z2-7]{32})", raw, re.IGNORECASE)
    if m32:
        try:
            return base64.b32decode(m32.group(1).upper()).hex().lower()
        except Exception:
            pass
    if HEX_INFOHASH_RE.fullmatch(raw):
        return raw.lower()
    if B32_INFOHASH_RE.fullmatch(raw):
        try:
            return base64.b32decode(raw.upper()).hex().lower()
        except Exception:
            pass
    return None


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
        resolved = resolve_redirect_url(raw)
        return resolved, title

    # Case 2: Pure infohash
    if is_infohash(raw):
        hash_val = extract_infohash(raw)
        title = f"Torrent ({hash_val[:8]})" if hash_val else "Torrent Stream"
        tb_token = getattr(config, "TORBOX_TOKEN", "")
        if hash_val and tb_token:
            resolved = resolve_torbox_hash(tb_token, hash_val)
            if not resolved or not resolved.startswith(("http://", "https://")):
                stremio_url = f"https://torrentio.strem.fun/torbox={tb_token}/resolve/torbox/{tb_token}/{hash_val}/0/0"
                resolved = resolve_redirect_url(stremio_url)
            if resolved and resolved.startswith(("http://", "https://")):
                log.info("[TORBOX RESOLVE] Infohash %s resolved to CDN stream: %s", hash_val[:8], resolved[:60])
                return resolved, title
        magnet = f"magnet:?xt=urn:btih:{hash_val}" if hash_val else raw
        return magnet, title

    # Case 3: Standard magnet link
    if is_magnet_link(raw):
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        dn = parsed.get("dn", ["Magnet Stream"])[0]
        hash_val = extract_infohash(raw)
        tb_token = getattr(config, "TORBOX_TOKEN", "")
        if hash_val and tb_token:
            resolved = resolve_torbox_hash(tb_token, hash_val)
            if not resolved or not resolved.startswith(("http://", "https://")):
                stremio_url = f"https://torrentio.strem.fun/torbox={tb_token}/resolve/torbox/{tb_token}/{hash_val}/0/0"
                resolved = resolve_redirect_url(stremio_url)
            if resolved and resolved.startswith(("http://", "https://")):
                log.info("[TORBOX RESOLVE] Magnet link %s resolved to CDN stream: %s", hash_val[:8], resolved[:60])
                return resolved, dn
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
        search_url = f"https://v3-cinemeta.strem.io/catalog/movie/top/search={urllib.parse.quote(clean_query)}.json"
        req = urllib.request.Request(search_url, headers=BROWSER_HEADERS)
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
            debrid_config = getattr(config, "TORRENTIO_CONFIG", "")
            prefix = f"{debrid_config}/" if debrid_config else ""
            torrentio_url = f"https://torrentio.strem.fun/{prefix}stream/movie/{imdb_id}.json"
            req = urllib.request.Request(torrentio_url, headers=BROWSER_HEADERS)
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


KNOWN_JEWISH_ISRAELI_DIRECTORS = {
    "taika waititi", "steven spielberg", "stanley kubrick", "woody allen", "roman polanski",
    "ethan coen", "joel coen", "coen brothers", "ari folman", "gideon raff", "darren aronofsky",
    "david cronenberg", "sam mendes", "billy wilder", "fritz lang", "mel brooks", "sidney lumet",
    "william wyler", "otto preminger", "michael curtiz", "ernst lubitsch", "fred zinnemann",
    "mike nichols", "rob reiner", "carl reiner", "barbra streisand", "spike jonze",
    "bryan singer", "jon favreau", "j.j. abrams", "todd phillips", "david o. russell", "sam raimi",
    "judd apatow", "seth rogen", "evan goldberg", "eli roth", "joseph gordon-levitt",
    "noah baumbach", "james gray", "susanne bier", "errol morris", "chantal akerman", "claude lanzmann",
    "amos gitai", "eitan fox", "joseph cedar", "samuel maoz", "nadav lapid", "guy nattiv",
    "hagai levi", "david shore", "david chase", "david simon", "max landis", "john landis",
    "harold ramis", "ivan reitman", "jason reitman",
}

_ISRAELI_DIRECTOR_CACHE: dict[str, bool] = {}


def is_israeli_or_jewish_person(name: str) -> bool:
    """Checks via known list, Wikidata, and Wikipedia whether a director/person has Israeli citizenship or Jewish religion/ethnicity."""
    if not name or not isinstance(name, str):
        return False
    clean_name = name.strip()
    if not clean_name:
        return False
    low_name = clean_name.lower()
    
    if low_name in _ISRAELI_DIRECTOR_CACHE:
        return _ISRAELI_DIRECTOR_CACHE[low_name]

    # Fast match against known directors set
    if any(k in low_name for k in KNOWN_JEWISH_ISRAELI_DIRECTORS):
        _ISRAELI_DIRECTOR_CACHE[low_name] = True
        return True

    # Check Wikidata API
    try:
        url = f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={urllib.parse.quote(clean_name)}&language=en&format=json"
        req_headers = {"User-Agent": "VaPlsBot/1.0 (https://github.com/dilelu94)"}
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode())
            results = data.get("search", [])
            if results:
                entity_id = results[0]["id"]
                entity_url = f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
                req2 = urllib.request.Request(entity_url, headers=req_headers)
                with urllib.request.urlopen(req2, timeout=3.0) as resp2:
                    edata = json.loads(resp2.read().decode())
                    claims = edata.get("entities", {}).get(entity_id, {}).get("claims", {})

                    # P27: Country of citizenship (Q801 = Israel)
                    for c in claims.get("P27", []):
                        qid = c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                        if qid == "Q801":
                            _ISRAELI_DIRECTOR_CACHE[low_name] = True
                            return True

                    # P140: Religion (Q9268 = Judaism)
                    for c in claims.get("P140", []):
                        qid = c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                        if qid == "Q9268":
                            _ISRAELI_DIRECTOR_CACHE[low_name] = True
                            return True

                    # P172: Ethnic group (Q7325 = Jews, Q614725 = Israeli Jews, Q6122670 = Jewish New Zealanders, Q902167 = Jewish Americans, etc)
                    for c in claims.get("P172", []):
                        qid = c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                        if qid in ("Q7325", "Q614725", "Q6122670", "Q902167", "Q1029471"):
                            _ISRAELI_DIRECTOR_CACHE[low_name] = True
                            return True
    except Exception as e:
        log.debug("Wikidata lookup error for '%s': %s", clean_name, e)

    # Wikipedia REST API fallback
    try:
        wp_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(clean_name.replace(' ', '_'))}"
        req_wp = urllib.request.Request(wp_url, headers={"User-Agent": "VaPlsBot/1.0 (https://github.com/dilelu94)"})
        with urllib.request.urlopen(req_wp, timeout=3.0) as rwp:
            wp_data = json.loads(rwp.read().decode())
            extract = wp_data.get("extract", "").lower()
            if "jewish" in extract or "israeli" in extract or "judaism" in extract:
                _ISRAELI_DIRECTOR_CACHE[low_name] = True
                return True
    except Exception:
        pass

    _ISRAELI_DIRECTOR_CACHE[low_name] = False
    return False


def check_israeli_flag(directors: list[str], country_raw: str = "") -> bool:
    """Returns True if production country is Israel or any director is Israeli / Jewish."""
    if country_raw and "israel" in str(country_raw).lower():
        return True
    for d in directors:
        if is_israeli_or_jewish_person(d):
            return True
    return False


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
        endpoints.append(("movie", f"https://v3-cinemeta.strem.io/catalog/movie/top/search={encoded}.json"))
    if type_filter in ("all", "series"):
        endpoints.append(("series", f"https://v3-cinemeta.strem.io/catalog/series/top/search={encoded}.json"))
    if type_filter in ("all", "anime"):
        endpoints.append(("anime", f"https://anime-kitsu.strem.fun/catalog/anime/kitsu-anime-list/search={encoded}.json"))

    for cat_type, url in endpoints:
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
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

                    raw_dir = item.get("director")
                    dirs = raw_dir if isinstance(raw_dir, list) else ([raw_dir] if raw_dir else [])
                    country = str(item.get("country") or "")
                    is_israeli = check_israeli_flag(dirs, country)

                    results.append({
                        "id": item_id,
                        "type": item_type,
                        "title": item.get("name", "Desconocido"),
                        "poster": poster,
                        "banner": banner,
                        "year": year,
                        "description": description,
                        "director": dirs,
                        "is_israeli": is_israeli,
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
        url = f"https://v3-cinemeta.strem.io/meta/{item_type}/{item_id}.json"

    meta_data = {}
    try:
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
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

    runtime_raw = str(meta_data.get("runtime") or "")
    runtime_mins = 0
    if runtime_raw:
        m_rt = re.search(r"(\d+)", runtime_raw)
        if m_rt:
            runtime_mins = int(m_rt.group(1))

    raw_director = meta_data.get("director")
    director = raw_director if isinstance(raw_director, list) else ([raw_director] if raw_director else [])

    raw_cast = meta_data.get("cast")
    cast = raw_cast if isinstance(raw_cast, list) else ([raw_cast] if raw_cast else [])

    raw_writer = meta_data.get("writer")
    writer = raw_writer if isinstance(raw_writer, list) else ([raw_writer] if raw_writer else [])

    imdb_rating = str(meta_data.get("imdbRating") or "")
    country_raw = str(meta_data.get("country") or "")
    is_israeli = check_israeli_flag(director, country_raw)

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
        "director": director,
        "cast": cast,
        "writer": writer,
        "imdb_rating": imdb_rating,
        "is_israeli": is_israeli,
        "episodes": episodes,
        "runtime": runtime_mins,
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
    debrid_config = getattr(config, "TORRENTIO_CONFIG", "")
    prefix = f"{debrid_config}/" if debrid_config else ""

    series_imdb = item_id if item_id.startswith("tt") else (imdb_id if imdb_id and imdb_id.startswith("tt") else None)
    if not series_imdb and item_id.startswith("kitsu:"):
        meta = get_stremio_meta_sync(item_type, item_id)
        series_imdb = meta.get("imdb_id")

    provider_bases = [
        "https://torrentio.strem.fun/",
    ]

    urls_to_try = []
    for b in provider_bases:
        if series_imdb:
            if item_type == "movie":
                urls_to_try.append(f"{b}{prefix}stream/movie/{series_imdb}.json")
                urls_to_try.append(f"{b}stream/movie/{series_imdb}.json")
            elif item_type == "series":
                urls_to_try.append(f"{b}{prefix}stream/series/{series_imdb}:{season}:{episode}.json")
                urls_to_try.append(f"{b}stream/series/{series_imdb}:{season}:{episode}.json")
            else: # anime or all
                urls_to_try.append(f"{b}{prefix}stream/series/{series_imdb}:{season}:{episode}.json")
                urls_to_try.append(f"{b}stream/series/{series_imdb}:{season}:{episode}.json")
                urls_to_try.append(f"{b}{prefix}stream/movie/{series_imdb}.json")
                urls_to_try.append(f"{b}stream/movie/{series_imdb}.json")

        if item_id.startswith("kitsu:"):
            urls_to_try.append(f"{b}{prefix}stream/series/{item_id}:{episode}.json")
            urls_to_try.append(f"{b}stream/series/{item_id}:{episode}.json")

    streams_out = []
    seen_urls = set()

    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                tdata = json.loads(resp.read().decode())
                raw_streams = tdata.get("streams", [])
                for s in raw_streams:
                    direct_url = s.get("url", "")
                    infohash = s.get("infoHash", "")
                    name_raw = str(s.get("name", ""))
                    title_raw = str(s.get("title", name_raw or "Torrent Stream"))
                    combined_check = f"{name_raw} {title_raw}".lower()

                    if not direct_url and not infohash:
                        continue
                    if "[❌]" in name_raw or "[❌]" in title_raw or "deprecated" in combined_check or "[😿]" in name_raw:
                        continue
                    if direct_url and ("/configure" in direct_url or "invalid_config" in direct_url or "elfhosted.com/playback" in direct_url):
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

