"""Image generation via Hugging Face Inference API (free tier).

Requires a Hugging Face account and API token:
1. Sign up at https://huggingface.co/join
2. Create a read token at https://huggingface.co/settings/tokens
3. Set HUGGINGFACE_API_TOKEN in .env

Uses black-forest-labs/FLUX.1-schnell by default, with automatic retry
when the model is cold-loading. Falls back to SD3 Medium if FLUX fails.
"""

import asyncio
import logging
import os
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger("bot.huggingface.image")

DEFAULT_MODEL = "black-forest-labs/FLUX.1-schnell"
FALLBACK_MODEL = "stabilityai/stable-diffusion-3-medium-diffusers"
MAX_FILE_SIZE = 8 * 1024 * 1024
TIMEOUT = 60
MAX_RETRIES = 5


async def _refine_prompt_with_gemini(prompt: str) -> str:
    """Uses Gemini to translate and optimize the image generation prompt to English.

    If Gemini is not configured, fails, or returns empty, falls back to the original prompt.
    """
    import geminiClient
    import config

    try:
        keys = geminiClient._pool_keys()
        picked = geminiClient._pick_key()
        picked_suffix = f"…{picked[-6:]}" if picked else "None"
        logger.info(
            "Gemini configurado. Keys en pool: %d. Key seleccionada: %s",
            len(keys),
            picked_suffix,
        )
    except Exception as e:
        logger.warning("Error al inspeccionar pool de keys de Gemini: %s", e)
        keys = []

    if not keys:
        logger.info("Gemini no configurado para traducción de prompt, usando prompt original")
        return prompt

    sys_inst = (
        "You are a professional prompt translator and engineer. "
        "Translate the input prompt to English for a text-to-image AI (FLUX.1-schnell). "
        "If the input is in Spanish or another language, translate it accurately to English. "
        "If it is already in English, output it as-is. "
        "Output ONLY the translated/refined English prompt. "
        "Do not include any introductions, explanations, markdown quotes, or conversational filler."
    )

    try:
        logger.info("refinando prompt con Gemini: %.100s", prompt)
        reply = await geminiClient.generate(
            user_message=prompt,
            system_instruction=sys_inst,
            model=config.GEMINI_MODEL,
            timeout_sec=10,
        )
        refined = reply.text.strip()
        if refined:
            logger.info("prompt refinado por Gemini: %.100s", refined)
            return refined
    except Exception as e:
        logger.warning("Fallo al refinar prompt con Gemini, usando original: %s", e)

    return prompt


async def generate(prompt: str, token: str) -> Optional[str]:
    if not token:
        logger.error("HUGGINGFACE_API_TOKEN no configurado")
        return None

    refined_prompt = await _refine_prompt_with_gemini(prompt)

    data = await _try_model(DEFAULT_MODEL, refined_prompt, token)
    if data is None:
        logger.info("falling back to %s", FALLBACK_MODEL)
        data = await _try_model(FALLBACK_MODEL, refined_prompt, token)
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
    url = f"https://router.huggingface.co/hf-inference/models/{model}"
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

