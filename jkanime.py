"""JKAnime (jkanime.net) scraper and stream extractor for GoLive streaming."""

import logging
import re
from typing import Optional
from urllib.parse import quote, urljoin
import aiohttp

log = logging.getLogger("jkanime")

BASE_URL = "https://jkanime.net/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
}


def is_jkanime_url(url: str) -> bool:
    """Return True if url is a valid JKAnime (jkanime.net) URL."""
    if not isinstance(url, str):
        return False
    clean = url.strip().lower()
    return clean.startswith(("http://", "https://")) and "jkanime.net" in clean


async def _fetch(
    session: aiohttp.ClientSession, url: str, referer: Optional[str] = None
) -> str:
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    timeout = aiohttp.ClientTimeout(total=15)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        if resp.status >= 400:
            log.warning("JKAnime HTTP %s for %s", resp.status, url)
            return ""
        return await resp.text()


async def search_jkanime(
    query: str, session: Optional[aiohttp.ClientSession] = None
) -> list[dict]:
    """Search JKAnime for anime titles matching query.

    Returns:
        List of dicts: ``[{"title": str, "slug": str, "url": str, "image": str}]``
    """
    if not query or not query.strip():
        return []

    q_clean = query.strip()
    search_url = urljoin(BASE_URL, f"buscar/{quote(q_clean)}/")

    async def _do_search(s: aiohttp.ClientSession) -> str:
        return await _fetch(s, search_url)

    if session is not None:
        html = await _do_search(session)
    else:
        async with aiohttp.ClientSession() as s:
            html = await _do_search(s)

    if not html:
        return []

    results: list[dict] = []
    seen_slugs: set[str] = set()

    # Pattern matching anime cards in JKAnime search results
    matches = re.finditer(
        r'<h5>\s*<a\s+href="https://jkanime\.net/([a-z0-9\-]+)/"[^>]*>([^<]+)</a>\s*</h5>',
        html,
        re.I,
    )
    for m in matches:
        slug = m.group(1).strip()
        title = m.group(2).strip()
        if (
            slug
            and slug
            not in (
                "buscar",
                "directorio",
                "estrenos",
                "top",
                "programacion",
                "horario",
                "aplicacion",
                "notificaciones",
            )
            and slug not in seen_slugs
        ):
            seen_slugs.add(slug)
            img_url = f"https://cdn.jkdesa.com/assets/images/animes/image/{slug}.jpg"
            results.append(
                {
                    "title": title,
                    "slug": slug,
                    "url": f"https://jkanime.net/{slug}/",
                    "image": img_url,
                }
            )

    # Secondary pattern matching nested card h5
    if not results:
        card_matches = re.finditer(
            r'<a[^>]+href="https://jkanime\.net/([a-z0-9\-]+)/"[^>]*>\s*<h5>([^<]+)</h5>',
            html,
            re.I,
        )
        for m in card_matches:
            slug = m.group(1).strip()
            title = m.group(2).strip()
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                img_url = f"https://cdn.jkdesa.com/assets/images/animes/image/{slug}.jpg"
                results.append(
                    {
                        "title": title,
                        "slug": slug,
                        "url": f"https://jkanime.net/{slug}/",
                        "image": img_url,
                    }
                )

    # Fallback pattern if <h5> is structured differently
    if not results:
        fallback_matches = re.finditer(
            r'href="https://jkanime\.net/([a-z0-9\-]+)/"[^>]*title="([^"]+)"',
            html,
            re.I,
        )
        for m in fallback_matches:
            slug = m.group(1).strip()
            title = m.group(2).strip()
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                img_url = f"https://cdn.jkdesa.com/assets/images/animes/image/{slug}.jpg"
                results.append(
                    {
                        "title": title,
                        "slug": slug,
                        "url": f"https://jkanime.net/{slug}/",
                        "image": img_url,
                    }
                )

    log.info("JKAnime search for %r returned %d results", query, len(results))
    return results


async def get_jkanime_anime_info(
    url_or_slug: str, session: Optional[aiohttp.ClientSession] = None
) -> dict:
    """Fetch metadata and total episode count for an anime on JKAnime.

    Returns:
        Dict: ``{"title": str, "slug": str, "url": str, "image": str, "episodes": int}``
    """
    if url_or_slug.startswith(("http://", "https://")):
        slug = url_or_slug.rstrip("/").rsplit("/", 1)[-1]
    else:
        slug = url_or_slug.strip().strip("/")

    anime_url = f"https://jkanime.net/{slug}/"

    async def _do_fetch(s: aiohttp.ClientSession) -> str:
        return await _fetch(s, anime_url)

    if session is not None:
        html = await _do_fetch(session)
    else:
        async with aiohttp.ClientSession() as s:
            html = await _do_fetch(s)

    title = slug.replace("-", " ").title()
    image = f"https://cdn.jkdesa.com/assets/images/animes/image/{slug}.jpg"
    total_episodes = 1

    if html:
        # Extract title from <h1> or <title>
        h1 = re.search(r'<h1>(.*?)</h1>', html, re.I)
        if h1:
            title = re.sub(r'<[^>]+>', '', h1.group(1)).strip()

        # Extract poster image
        img_match = re.search(
            r'<img[^>]+src="(https://cdn\.jkdesa\.com/assets/images/animes/image/[^"]+)"',
            html,
            re.I,
        )
        if img_match:
            image = img_match.group(1)

        # Extract episode count (e.g. <li><span>Episodios:</span> 12</li>)
        ep_match = re.search(r'<span>Episodios:</span>\s*(\d+)', html, re.I)
        if ep_match:
            total_episodes = int(ep_match.group(1))
        else:
            # Check maximum episode number found in episode links
            ep_nums = re.findall(
                rf'href="https://jkanime\.net/{re.escape(slug)}/(\d+)/"', html
            )
            if ep_nums:
                total_episodes = max([int(n) for n in ep_nums])

    return {
        "title": title,
        "slug": slug,
        "url": anime_url,
        "image": image,
        "episodes": max(1, total_episodes),
    }


async def extract_jkanime_stream(
    episode_url: str, session: Optional[aiohttp.ClientSession] = None
) -> tuple[Optional[str], str]:
    """Extract direct stream URL (.m3u8 or .mp4) from a JKAnime episode page.

    Returns:
        (stream_url, episode_title)
    """
    clean_url = episode_url.strip()
    parts = clean_url.rstrip("/").rsplit("/", 2)
    slug = parts[-2] if len(parts) >= 2 else "anime"
    ep_num = parts[-1] if len(parts) >= 1 and parts[-1].isdigit() else "1"
    ep_title = f"{slug.replace('-', ' ').title()} - Episodio {ep_num}"

    async def _do_extract(s: aiohttp.ClientSession) -> tuple[Optional[str], str]:
        html = await _fetch(s, clean_url)
        if not html:
            return None, ep_title

        # Find title if present
        h1 = re.search(r'<h1>(.*?)</h1>', html, re.I)
        if h1:
            ep_title = re.sub(r'<[^>]+>', '', h1.group(1)).strip()

        # Gather player iframe URLs inside /jkplayer/*
        iframes = re.findall(r'<iframe[^>]+src="([^"]+)"', html, re.I)
        script_iframes = re.findall(
            r'src=["\'](https://jkanime\.net/jkplayer/[^"\']+)["\']', html
        )
        player_urls = list(
            dict.fromkeys(
                [u for u in iframes if "jkplayer" in u] + script_iframes
            )
        )

        log.info(
            "Extracting JKAnime stream for %s: found %d player URLs",
            ep_title,
            len(player_urls),
        )

        for player_url in player_urls:
            try:
                p_html = await _fetch(s, player_url, referer=clean_url)
                if not p_html:
                    continue

                # Search for .m3u8 stream URL
                m3u8_matches = re.findall(
                    r'https?://[^\s\'"<>\\]+\.m3u8[^\s\'"<>\\]*', p_html
                )
                if m3u8_matches:
                    log.info("Found .m3u8 stream via %s", player_url[:60])
                    return m3u8_matches[0], ep_title

                # Search for .mp4 stream URL
                mp4_matches = re.findall(
                    r'https?://[^\s\'"<>\\]+\.mp4[^\s\'"<>\\]*', p_html
                )
                if mp4_matches:
                    log.info("Found .mp4 stream via %s", player_url[:60])
                    return mp4_matches[0], ep_title

                # Search for DPlayer stream URL (url: 'https://...')
                dp_match = re.search(
                    r"url:\s*['\"](https?://[^\s'\"<>\\]+)['\"]", p_html
                )
                if dp_match:
                    log.info("Found DPlayer stream via %s", player_url[:60])
                    return dp_match.group(1), ep_title

            except Exception as e:
                log.warning("Player fetch failed for %s: %s", player_url, e)

        return None, ep_title

    if session is not None:
        return await _do_extract(session)
    else:
        async with aiohttp.ClientSession() as s:
            return await _do_extract(s)
