"""Slash command logic for Instagram Reel streaming via GoLive relay.

Provides two entry points:

* ``start_instagram_stream_logic()`` — infinite-scroll feed mode.
  Discovers reel URLs via yt-dlp ``flat_playlist`` and extracts each
  reel with video+audio DASH streams.  No credentials needed.

* ``start_instagram_reel_stream_logic()`` — single-reel mode that extracts
  the video via yt-dlp (no credentials needed).
"""

import logging

import aiohttp
from urllib.parse import urljoin

import config

log = logging.getLogger(__name__)


async def start_instagram_stream_logic(
    guild_id: int,
    voice_channel,
) -> tuple[bool, str]:
    """Sends the HTTP request to the GoLive relay to start Instagram streaming.

    The relay (golive/bot.py) handles yt-dlp feed discovery, GoLive
    connection, and the infinite reel extraction loop.

    Returns:
        (success, status_message)
    """
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        return False, "❌ El relay GoLive no está configurado."

    url = urljoin(config.GOLIVE_RELAY_URL, "/instagram")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {
        "guild_id": guild_id,
        "channel_id": voice_channel.id,
    }
    log.info(
        "[INSTAGRAM_LOGIC] POST %s guild=%s channel=%s",
        url, guild_id, voice_channel.id,
    )
    timeout = aiohttp.ClientTimeout(total=config.GOLIVE_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning("instagram relay HTTP %s: %s", resp.status, body[:200])
                    return False, f"⚠️ No pude iniciar el stream de Instagram (HTTP {resp.status})."
    except Exception as e:
        log.exception("instagram relay failed")
        return False, f"⚠️ Error iniciando stream de Instagram: {e}"

    return True, f"📱 Transmitiendo Reels de Instagram en **{voice_channel.name}**.\nUsá **/stopstream** para cortar."


async def start_instagram_reel_stream_logic(
    guild_id: int,
    voice_channel,
    reel_url: str,
) -> tuple[bool, str]:
    """Sends a specific Instagram Reel URL to the GoLive relay for streaming.

    The relay extracts the video/audio URLs via yt-dlp (no Instagram
    credentials needed), connects via GoLive with vertical letterboxing,
    and plays the reel once.

    Returns:
        (success, status_message)
    """
    if not (config.GOLIVE_RELAY_URL and config.GOLIVE_RELAY_SECRET):
        return False, "❌ El relay GoLive no está configurado."

    url = urljoin(config.GOLIVE_RELAY_URL, "/instagram")
    headers = {"X-API-Secret": config.GOLIVE_RELAY_SECRET}
    payload = {
        "guild_id": guild_id,
        "channel_id": voice_channel.id,
        "url": reel_url,
    }
    log.info(
        "[INSTAGRAM_REEL_LOGIC] POST %s guild=%s channel=%s url=%s",
        url, guild_id, voice_channel.id, reel_url[:80],
    )
    timeout = aiohttp.ClientTimeout(total=config.GOLIVE_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    log.warning("instagram reel relay HTTP %s: %s", resp.status, body[:200])
                    return False, f"⚠️ No pude iniciar el stream del reel (HTTP {resp.status})."
    except Exception as e:
        log.exception("instagram reel relay failed")
        return False, f"⚠️ Error iniciando stream del reel: {e}"

    return True, f"📱 Reproduciendo reel de Instagram en **{voice_channel.name}**.\nUsá **/stopstream** para cortar."
