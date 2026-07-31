"""Extrae un frame de un video de Reel para contexto visual.

El cloud no tiene sesión de Instagram, así que descarga el video_url del CDN
(enviado por el scraper local) con ffmpeg y emite un único frame JPEG (~30% de
la duración). Devuelve None ante cualquier fallo para que el llamador pueda
usar el thumbnail como fallback.
"""

import asyncio
import logging
import shutil

logger = logging.getLogger("reelFrame")

FFMPEG_BIN = "/usr/local/bin/ffmpeg"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def grab_frame(video_url: str, duration: float | None = None) -> bytes | None:
    """Toma un frame JPEG de un reel. None ante cualquier fallo."""
    bin_path = shutil.which("ffmpeg") or FFMPEG_BIN
    pos = _frame_offset(duration)
    cmd = [
        bin_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-user_agent",
        _UA,
        "-headers",
        "Referer: https://www.instagram.com/\r\n",
        "-ss",
        str(pos),
        "-t",
        "30",
        "-i",
        video_url,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logger.warning("[REEL-FRAME] no se pudo lanzar ffmpeg para %s: %s", _redact(video_url), e)
        return None

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        logger.warning("[REEL-FRAME] ffmpeg timeout para %s", _redact(video_url))
        return None

    if proc.returncode != 0 or not out:
        logger.warning(
            "[REEL-FRAME] ffmpeg falló (rc=%s) para %s: %s",
            proc.returncode,
            _redact(video_url),
            (err or b"").decode(errors="replace")[:300],
        )
        return None

    logger.info(
        "[REEL-FRAME] frame obtenido (%d bytes, %.1fs) para %s",
        len(out),
        pos,
        _redact(video_url),
    )
    return out


def _frame_offset(duration: float | None) -> float:
    """30% de la duración, limitado a [1, duración-1]."""
    if not duration or duration < 2:
        return 3.0
    pos = duration * 0.3
    return max(1.0, min(pos, duration - 1.0))


def _redact(url: str) -> str:
    return (url or "")[:120]
