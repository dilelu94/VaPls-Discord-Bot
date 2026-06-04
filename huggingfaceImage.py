"""Image generation via Hugging Face Inference API (free tier).

Requires a Hugging Face account and API token:
1. Sign up at https://huggingface.co/join
2. Create a read token at https://huggingface.co/settings/tokens
3. Set HUGGINGFACE_API_TOKEN in .env

Uses black-forest-labs/FLUX.1-dev by default, with automatic retry
when the model is cold-loading. Falls back to SDXL if FLUX fails.
"""

import asyncio
import logging
import os
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger("bot.huggingface.image")

DEFAULT_MODEL = "black-forest-labs/FLUX.1-dev"
FALLBACK_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
MAX_FILE_SIZE = 8 * 1024 * 1024
TIMEOUT = 60
MAX_RETRIES = 5


async def generate(prompt: str, token: str) -> Optional[str]:
    if not token:
        logger.error("HUGGINGFACE_API_TOKEN no configurado")
        return None

    data = await _try_model(DEFAULT_MODEL, prompt, token)
    if data is None:
        logger.info("falling back to %s", FALLBACK_MODEL)
        data = await _try_model(FALLBACK_MODEL, prompt, token)
    if data is None:
        return None
    if len(data) > MAX_FILE_SIZE:
        logger.warning("imagen %d bytes supera el límite de 8 MB", len(data))
        return None
    fd, path = tempfile.mkstemp(suffix=".png", prefix="hfimg_")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    logger.info("imagen guardada en %s (%d bytes)", path, len(data))
    return path


async def _try_model(model: str, prompt: str, token: str) -> Optional[bytes]:
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(
                "intentando %s, intento %d/%d con prompt: %.100s",
                model,
                attempt + 1,
                MAX_RETRIES,
                prompt,
            )
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    headers=headers,
                    json={"inputs": prompt},
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("Content-Type", "")
                        if "image/" in ct:
                            return await resp.read()
                        body = await resp.text()
                        logger.warning(
                            "%s returned 200 with Content-Type=%s: %.200s",
                            model,
                            ct,
                            body,
                        )
                        return None
                    body = await resp.text()
                    if "currently loading" in body or "ModelTooBusy" in body:
                        wait = min(2**attempt * 2, 30)
                        logger.info(
                            "%s cargando, reintento %d/%d en %ds",
                            model,
                            attempt + 1,
                            MAX_RETRIES,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.error("%s HTTP %d: %.200s", model, resp.status, body)
                    return None
        except asyncio.TimeoutError:
            logger.warning(
                "%s timeout (intento %d/%d)", model, attempt + 1, MAX_RETRIES
            )
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2)
                continue
        except Exception:
            logger.exception(
                "%s falló (intento %d/%d)", model, attempt + 1, MAX_RETRIES
            )
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2)
                continue
    logger.error("%s agotó reintentos", model)
    return None


async def generarimagenLogic(ctx, prompt: str):
    """Encapsulates the /generarimagen slash command logic.

    Checks the prompt, configures the Hugging Face token, calls the generator,
    sends the resulting image file to Discord, and ensures the temporary file
    is deleted.
    """
    import config
    import discord
    from bot import safe_defer, safe_respond, safeEdit

    if not prompt or not prompt.strip():
        await safe_respond(ctx, "decime qué generar")
        return
    if not config.HUGGINGFACE_API_TOKEN:
        await safe_respond(
            ctx,
            "❌ El token de Hugging Face no está configurado. "
            "Avisale al admin para que lo agregue al .env",
        )
        return
    await safe_defer(ctx)
    await safeEdit(ctx, "⏳ Generando imagen...")
    path = await generate(prompt, config.HUGGINGFACE_API_TOKEN)
    if path is None:
        await safeEdit(ctx, "❌ No pude generar la imagen. Probá de nuevo más tarde.")
        return
    try:
        await ctx.interaction.edit_original_response(
            content="",
            file=discord.File(path, filename="imagen.png"),
        )
    except discord.HTTPException as e:
        if "file is too large" in str(e).lower() or "413" in str(e):
            await safeEdit(ctx, "❌ La imagen supera el límite de 8 MB de Discord.")
        else:
            await safeEdit(ctx, f"❌ Error al enviar: {e}")
    finally:
        try:
            os.unlink(path)
        except Exception:
            logger.warning("could not delete temp image %s", path)

