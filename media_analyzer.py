"""Media and link content analyzer for VaPls Discord Bot.

Extracts middle frames from videos via ffprobe/ffmpeg, link metadata via
yt-dlp or OpenGraph tags, and analyzes the content with Gemini to distill user
tastes and interests for the Indio's long-term memory.
"""

import asyncio
import base64
import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp
import discord

import config
import geminiClient

logger = logging.getLogger("bot.media_analyzer")

_URL_RE = re.compile(r"https?://[^\s<]+", re.IGNORECASE)
_DISCORD_CDN_EMOJI_RE = re.compile(r"cdn\.discordapp\.com/emojis/", re.IGNORECASE)

# Ignored domains/URLs (utility links, internal bot links, basic emojis)
_IGNORED_DOMAINS = {
    "tenor.com",
    "giphy.com",
    "cdn.discordapp.com/emojis",
    "media.discordapp.net/emojis",
}


def is_analyzable_url(url: str) -> bool:
    """Check if a URL is valid for content/interest analysis."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    lower = url.lower()
    if any(domain in lower for domain in _IGNORED_DOMAINS):
        return False
    return True


async def get_video_duration(video_url: str) -> Optional[float]:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode == 0 and stdout:
            val = float(stdout.decode().strip())
            if val > 0:
                return val
    except Exception as e:
        logger.debug("ffprobe failed for %s: %e", video_url, e)
    return None


async def extract_video_middle_frame(video_url: str) -> Optional[bytes]:
    """Extract a single JPEG frame from the middle of a video using ffmpeg."""
    duration = await get_video_duration(video_url)
    target_time = (duration / 2.0) if (duration and duration > 1.0) else 1.0

    cmd = [
        "ffmpeg",
        "-ss",
        f"{target_time:.2f}",
        "-i",
        video_url,
        "-vframes",
        "1",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        if proc.returncode == 0 and stdout and stdout.startswith(b"\xff\xd8"):
            return stdout
    except Exception as e:
        logger.debug("ffmpeg frame extraction failed for %s: %e", video_url, e)
    return None


async def extract_link_metadata(url: str) -> Optional[Dict[str, Any]]:
    """Extract metadata (title, description, channel/author) from a web link.

    Uses yt-dlp first for supported video/social media platforms, falling back to
    OpenGraph meta tags via aiohttp.
    """
    if not is_analyzable_url(url):
        return None

    # 1. Try yt-dlp for video/social links (YouTube, TikTok, Twitter, Twitch, etc.)
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-warnings",
        "--playlist-items",
        "1",
        url,
    ]
    if getattr(config, "YT_DLP_POT_BASE_URL", None):
        cmd.extend(["--extractor-args", f"youtube:pot_base_url={config.YT_DLP_POT_BASE_URL}"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=12.0)
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            title = data.get("title") or data.get("fulltitle")
            uploader = data.get("uploader") or data.get("channel") or data.get("uploader_id")
            description = data.get("description") or ""
            tags = data.get("tags") or []

            if title:
                return {
                    "url": url,
                    "title": title,
                    "uploader": uploader,
                    "description": description[:300] if description else "",
                    "tags": tags[:5] if isinstance(tags, list) else [],
                    "platform": data.get("extractor_key") or "video",
                }
    except Exception:
        pass

    # 2. Fallback: HTTP GET OpenGraph / HTML title extraction
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=6.0),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    ct = (resp.headers.get("Content-Type") or "").lower()
                    if "text/html" in ct or "application/xhtml+xml" in ct:
                        # Read up to 256KB to find metadata
                        html = await resp.content.read(262144)
                        text = html.decode("utf-8", errors="ignore")

                        og_title = _extract_meta(text, "og:title") or _extract_tag(text, "title")
                        og_desc = _extract_meta(text, "og:description") or _extract_meta(text, "description")
                        og_site = _extract_meta(text, "og:site_name")

                        if og_title:
                            return {
                                "url": url,
                                "title": og_title.strip(),
                                "uploader": og_site,
                                "description": (og_desc.strip()[:300] if og_desc else ""),
                                "platform": "web",
                            }
    except Exception as e:
        logger.debug("OpenGraph extraction failed for %s: %e", url, e)

    return None


def _extract_meta(html: str, property_name: str) -> Optional[str]:
    """Helper to extract <meta property="..." content="..."> or <meta name="...">."""
    pattern = (
        rf'<meta\s+[^>]*?(?:property|name)=["\']{re.escape(property_name)}["\']\s+content=["\'](.*?)["\']'
    )
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not match:
        pattern_rev = (
            rf'<meta\s+[^>]*?content=["\'](.*?)["\']\s+(?:property|name)=["\']{re.escape(property_name)}["\']'
        )
        match = re.search(pattern_rev, html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_tag(html: str, tag_name: str) -> Optional[str]:
    """Helper to extract content inside <tag>content</tag>."""
    pattern = rf"<{tag_name}[^>]*>(.*?)</{tag_name}>"
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


async def analyze_content_interest(
    user_name: str,
    content_type: str,
    *,
    image_bytes: Optional[bytes] = None,
    link_meta: Optional[Dict[str, Any]] = None,
    image_mime: str = "image/jpeg",
) -> Optional[str]:
    """Use Gemini to summarize the user's taste/interest from media or link metadata.

    Returns a 1-sentence summary of the user's taste (or None if analysis failed).
    """
    system_instruction = (
        "Sos un módulo de análisis de intereses para un bot de Discord. "
        f"Tu tarea es analizar el contenido ({content_type}) compartido por el usuario '{user_name}' "
        "y resumir en 1 sola frase corta, directa y natural en español cuál es el gusto, tema o interés "
        "que demuestra este contenido.\n\n"
        "Ejemplos de respuesta:\n"
        f"- A {user_name} le interesan los videos de gameplays de Minecraft.\n"
        f"- A {user_name} le gustan los memes de autos deportivos y tuning.\n"
        f"- A {user_name} le atrae el contenido sobre tecnología y programación.\n\n"
        "Reglas:\n"
        "1. La frase debe ser concisa (máximo 15 palabras).\n"
        "2. Debe comenzar mencionando al usuario (ej: 'A <nombre> le...').\n"
        "3. No agregues explicaciones extras, introducción ni formato markdown. Responde ÚNICAMENTE la frase de interés."
    )

    image_parts = None
    user_msg = f"Contenido enviado por {user_name}:"

    if image_bytes:
        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        image_parts = [{"inlineData": {"mimeType": image_mime, "data": b64_data}}]
        user_msg += " (ver imagen adjunta)"

    if link_meta:
        title = link_meta.get("title", "")
        uploader = link_meta.get("uploader", "")
        desc = link_meta.get("description", "")
        tags = ", ".join(link_meta.get("tags") or [])
        user_msg += f"\n- Enlace: {link_meta.get('url')}\n- Título: {title}"
        if uploader:
            user_msg += f"\n- Canal/Sitio: {uploader}"
        if desc:
            user_msg += f"\n- Descripción: {desc[:200]}"
        if tags:
            user_msg += f"\n- Etiquetas: {tags}"

    try:
        reply = await geminiClient.generate(
            user_message=user_msg,
            system_instruction=system_instruction,
            image_parts=image_parts,
            model=getattr(config, "GEMINI_MODEL_LITE", "gemini-2.5-flash-lite"),
            timeout_sec=20.0,
        )
        if reply and reply.text:
            cleaned = reply.text.strip().rstrip(".")
            if cleaned and not cleaned.startswith("HTTP"):
                return cleaned + "."
    except Exception as e:
        logger.warning("analyze_content_interest failed for user %s: %s", user_name, e)

    return None
