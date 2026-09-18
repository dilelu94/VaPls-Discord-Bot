"""Pollinations.ai Image Generation & Image-to-Image Editing module.

Provides free image generation and transformation capabilities via Pollinations.ai,
supporting text-to-image and image-to-image workflows with models like 'flux' and 'turbo'.
"""

import io
import logging
import urllib.parse
from typing import Optional

import aiohttp

logger = logging.getLogger("bot.pollinations")

POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt/"
DEFAULT_TIMEOUT = 35.0


async def generate_or_edit_image(
    prompt: str,
    image_url: Optional[str] = None,
    model: str = "flux",
    width: int = 1024,
    height: int = 1024,
) -> Optional[bytes]:
    """Fetch generated or transformed image bytes from Pollinations.ai.

    Args:
        prompt: Description of the desired image or transformation.
        image_url: Optional base image URL for image-to-image transformation.
        model: Model name ('flux', 'turbo', etc.).
        width: Image target width.
        height: Image target height.

    Returns:
        Image bytes (JPEG) or None on failure/timeout.
    """
    if not prompt or not prompt.strip():
        logger.warning("empty prompt provided to generate_or_edit_image")
        return None

    encoded_prompt = urllib.parse.quote(prompt.strip())
    query_params = {
        "model": model,
        "nologo": "true",
        "width": str(width),
        "height": str(height),
    }

    if image_url and image_url.strip():
        query_params["image"] = image_url.strip()

    url = f"{POLLINATIONS_BASE_URL}{encoded_prompt}?{urllib.parse.urlencode(query_params)}"
    logger.info("requesting pollinations image: %s", url)

    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) VaPls-Discord-Bot/1.0"
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("pollinations HTTP status %s", resp.status)
                    return None
                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type and "octet-stream" not in content_type:
                    logger.warning("unexpected content-type from pollinations: %s", content_type)
                data = await resp.read()
                if not data or len(data) < 10:
                    logger.warning("pollinations returned empty or tiny payload (%d bytes)", len(data))
                    return None
                logger.info("successfully fetched image (%d bytes)", len(data))
                return data
    except Exception as e:
        logger.exception("failed to fetch image from pollinations: %s", e)
        return None


async def imagenLogic(
    ctx,
    prompt: str,
    image_attachment=None,
    image_url: Optional[str] = None,
    modelo: str = "flux",
) -> None:
    """Encapsulates the /imagen slash command logic.

    Args:
        ctx: Discord application context.
        prompt: Description of the image or modification.
        image_attachment: Optional discord.Attachment object.
        image_url: Optional string URL.
        modelo: Selected AI model ('flux', 'turbo').
    """
    import discord
    from bot import safe_defer, safe_respond

    if not prompt or not prompt.strip():
        await safe_respond(ctx, "❌ Tenés que especificar una descripción o prompt.")
        return

    await safe_defer(ctx)

    # Resolve input image URL (attachment takes priority over string URL)
    target_image_url: Optional[str] = None
    if image_attachment is not None:
        target_image_url = getattr(image_attachment, "url", None)
    elif image_url and image_url.strip():
        target_image_url = image_url.strip()

    valid_models = ("flux", "turbo")
    selected_model = modelo.lower().strip() if modelo else "flux"
    if selected_model not in valid_models:
        selected_model = "flux"

    img_bytes = await generate_or_edit_image(
        prompt=prompt,
        image_url=target_image_url,
        model=selected_model,
    )

    if not img_bytes:
        await safe_respond(ctx, "❌ No pude generar/editar la imagen. Probá de nuevo más tarde.")
        return

    filename = "imagen_editada.jpg" if target_image_url else "imagen_generada.jpg"
    file_obj = discord.File(io.BytesIO(img_bytes), filename=filename)

    header_text = (
        f"🎨 **Imagen transformada** (`{selected_model}`)\n> **Prompt:** {prompt.strip()}"
        if target_image_url
        else f"🖼️ **Imagen generada** (`{selected_model}`)\n> **Prompt:** {prompt.strip()}"
    )

    try:
        await ctx.followup.send(content=header_text, file=file_obj)
    except Exception as e:
        logger.exception("failed to send image followup: %s", e)
        await safe_respond(ctx, f"⚠️ Error al enviar la imagen: {e}")
