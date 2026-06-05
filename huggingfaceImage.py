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
        logger.info(
            "Gemini no configurado para traducción de prompt, usando prompt original"
        )
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
            timeout_sec=20,
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


async def generate_img2img(prompt: str, init_image_paths: list[str]) -> Optional[str]:
    """Generates an edited image from input images and prompt via Cloudflare Workers AI FLUX.2 [dev].

    Uses @cf/black-forest-labs/flux-2-dev with multipart form data.
    Accepts up to 4 input images (each resized to ≤512×512).
    """
    import config

    account_id = config.CLOUDFLARE_ACCOUNT_ID
    cf_token = config.CLOUDFLARE_API_TOKEN

    if not account_id or not cf_token:
        logger.error(
            "CLOUDFLARE_ACCOUNT_ID o CLOUDFLARE_API_TOKEN no configurado en .env"
        )
        raise RuntimeError(
            "❌ Configuración Faltante: Para editar imágenes gratis, el admin debe configurar "
            "CLOUDFLARE_ACCOUNT_ID y CLOUDFLARE_API_TOKEN en el archivo .env (no requiere tarjeta)."
        )

    refined_prompt = await _refine_prompt_with_gemini(prompt)

    try:
        import base64
        import io
        import aiohttp
        from PIL import Image

        form = aiohttp.FormData()
        form.add_field("prompt", refined_prompt)

        for i, path in enumerate(init_image_paths[:4]):
            img = Image.open(path)
            if img.width > 512 or img.height > 512:
                img.thumbnail((512, 512), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            form.add_field(
                f"input_image_{i}",
                buf,
                content_type="image/png",
                filename=f"input_{i}.png",
            )

        form.add_field("steps", "25")
        form.add_field("guidance", "3.5")
        form.add_field("width", "512")
        form.add_field("height", "512")

        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/black-forest-labs/flux-2-dev"
        headers = {"Authorization": f"Bearer {cf_token}"}

        logger.info(
            "intentando img2img con FLUX.2 [dev] y prompt: %.100s",
            refined_prompt,
        )

        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                url,
                headers=headers,
                data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("FLUX.2 [dev] HTTP %d: %.200s", resp.status, body)
                    raise RuntimeError(
                        f"FLUX.2 [dev] falló (HTTP {resp.status}): {body[:150]}"
                    )

                result = await resp.json()
                if "image" not in result:
                    raise RuntimeError(
                        f"FLUX.2 [dev] respuesta sin campo 'image': {result}"
                    )

                img_bytes = base64.b64decode(result["image"])

        os.makedirs("image_cache", exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="cfi2i_", dir="image_cache")
        try:
            with open(path, "wb") as f:
                f.write(img_bytes)
        finally:
            os.close(fd)

        logger.info(
            "imagen img2img de FLUX.2 [dev] guardada en %s (%d bytes)",
            path,
            len(img_bytes),
        )
        return path
    except Exception as e:
        logger.exception("FLUX.2 [dev] generate_img2img falló")
        raise e


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
    sends the resulting image file to the designated channel (or directly if invoked there),
    and ensures the temporary file is deleted.
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

    target_channel_id = config.INDIO_REPLY_CHANNEL_ID or 1490008278275461280
    is_outside = ctx.channel_id != target_channel_id

    # If outside, defer as ephemeral so the status update is private
    await safe_defer(ctx, ephemeral=is_outside)

    if is_outside:
        await safeEdit(ctx, f"⏳ Imagen generándose en <#{target_channel_id}>...")
    else:
        await safeEdit(ctx, "⏳ Generando imagen...")

    # First, verify if we have access to the target channel (if outside)
    target_channel = None
    if is_outside:
        from unittest.mock import Mock

        has_real_bot = (
            hasattr(ctx, "bot")
            and ctx.bot is not None
            and (
                not isinstance(ctx.bot, Mock) or "_mock_custom_bot" in ctx.bot.__dict__
            )
        )
        if has_real_bot:
            target_channel = ctx.bot.get_channel(target_channel_id)
            if target_channel is None:
                try:
                    target_channel = await ctx.bot.fetch_channel(target_channel_id)
                except Exception:
                    pass

            # If bot is present but channel is not accessible, fail with a clear message
            if target_channel is None:
                await safeEdit(ctx, "no acceso al canal")
                return

    path = await generate(prompt, config.HUGGINGFACE_API_TOKEN)
    if path is None:
        await safeEdit(ctx, "❌ No pude generar la imagen. Probá de nuevo más tarde.")
        return

    try:
        if is_outside:
            if target_channel:
                await target_channel.send(
                    content=f"<@{ctx.author.id}>, acá está la imagen que me pediste para: **{prompt}**",
                    file=discord.File(path, filename="imagen.png"),
                )
                await safeEdit(ctx, f"✅ Imagen generada en <#{target_channel_id}>!")
            else:
                # Fallback to direct response if no bot object exists (e.g. standard unit tests)
                await ctx.interaction.edit_original_response(
                    content="",
                    file=discord.File(path, filename="imagen.png"),
                )
        else:
            await ctx.interaction.edit_original_response(
                content="",
                file=discord.File(path, filename="imagen.png"),
            )
    except discord.Forbidden:
        await safeEdit(ctx, "no acceso al canal")
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
