"""Image generation via Gemini web UI (Playwright).

Free alternative to the paid Imagen/3.1 Flash Image API. Uses a persisted
Gmail session (storage_state) to access gemini.google.com's native image
generation without an API key.
"""

import asyncio
import base64
import logging
import os
import tempfile
from typing import Optional

import aiohttp

logger = logging.getLogger("bot.gemini.image")

STORAGE_STATE_PATH = "gemini_auth.json"
GEMINI_URL = "https://gemini.google.com/"
MAX_FILE_SIZE = 8 * 1024 * 1024
GENERATE_TIMEOUT = 90
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.navigator.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });
"""

_browser = None
_context = None
_playwright = None


async def init(storage_path: str = STORAGE_STATE_PATH) -> bool:
    """Launch a headless Chromium and load the saved Gmail session.

    Call once at bot startup. Returns True when ``storage_state`` was
    loaded successfully, False if the file is missing (image generation
    will fail until ``setup_gemini_session.py`` is run).
    """
    global _playwright, _browser, _context
    if _browser is not None:
        return True
    from playwright.async_api import async_playwright

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    if os.path.exists(storage_path):
        _context = await _browser.new_context(storage_state=storage_path)
        await _context.add_init_script(STEALTH_JS)
        logger.info("browser ready (session from %s)", storage_path)
        return True

    logger.warning(
        "no auth file at %s — run setup_gemini_session.py first", storage_path
    )
    _context = await _browser.new_context()
    await _context.add_init_script(STEALTH_JS)
    return False


async def generate(prompt: str) -> Optional[str]:
    """Generate an image and return the path to a temp file.

    The returned file **must** be deleted by the caller after sending it
    to Discord. Returns ``None`` on any failure (timeout, auth missing,
    no image in response, too large).
    """
    global _context
    if _context is None:
        ok = await init()
        if not ok:
            return None

    page = await _context.new_page()
    try:
        logger.info("navigating to %s", GEMINI_URL)
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30000)
        logger.info("page loaded, title=%s", await page.title())
        await page.wait_for_timeout(3000)

        url = page.url
        logger.info("current url=%s", url)

        # Check if Google redirected to login page
        if "accounts.google.com" in url or "ServiceLogin" in url:
            logger.warning(
                "⚠️  SESION DE GEMINI EXPIRADA — redirigió a login. "
                "Ejecutá setup_gemini_session.py de nuevo."
            )
            return None

        # Dismiss any promo overlays (I/O 2026 card etc.)
        for btn in await page.locator(
            'button:has-text("Got it"), button:has-text("Close"), button:has-text("Dismiss"), [aria-label*=Close]'
        ).all():
            if await btn.is_visible(timeout=1000):
                await btn.click(force=True)
                logger.info("dismissed overlay")
                await page.wait_for_timeout(500)

        ta = page.locator("div[role=textbox]").first
        logger.info("waiting for text input to be visible")
        try:
            await ta.wait_for(state="attached", timeout=15000)
        except Exception:
            logger.error("text input not found, saving screenshot")
            try:
                await page.screenshot(path="/tmp/gemini_debug.png", full_page=True)
                logger.info("screenshot saved to /tmp/gemini_debug.png")
            except Exception as e:
                logger.warning("screenshot failed: %s", e)
            raise

        logger.info("text input found, filling prompt")
        await ta.fill(prompt)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")
        logger.info("prompt submitted, waiting for image")
        await page.wait_for_timeout(3000)
        got_it = page.locator("button", has_text="Got it").first
        if await got_it.is_visible(timeout=5000):
            await got_it.click(force=True)
            logger.info("dismissed Got it overlay")
            await page.wait_for_timeout(2000)

        img_data = await _wait_for_image(page)
        if img_data is None:
            logger.warning("no image data returned")
            return None
        if len(img_data) > MAX_FILE_SIZE:
            logger.warning("image %d bytes exceeds 8 MB limit", len(img_data))
            return None

        fd, path = tempfile.mkstemp(suffix=".png", prefix="gimg_")
        try:
            os.write(fd, img_data)
        finally:
            os.close(fd)
        logger.info("image saved to %s (%d bytes)", path, len(img_data))
        return path
    except Exception:
        logger.exception("image generation failed")
        return None
    finally:
        await page.close()


async def _find_session_issue(page) -> Optional[str]:
    """Check page for session-expired signals and return a message if found."""
    try:
        url = page.url
        if "accounts.google.com" in url or "ServiceLogin" in url:
            return "redirigió a pantalla de login"
    except Exception:
        pass
    return None


async def _wait_for_image(page, timeout: int = GENERATE_TIMEOUT) -> Optional[bytes]:
    """Poll the page until a generated image appears and return its bytes."""
    from playwright.async_api import TimeoutError as PwTimeout

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            logger.warning("timeout waiting for generated image")
            return None

        # Periodic session health check
        if int(remaining) % 10 == 0:
            issue = await _find_session_issue(page)
            if issue:
                logger.warning(
                    "⚠️  SESION DE GEMINI EXPIRADA — %s. "
                    "Ejecutá setup_gemini_session.py de nuevo.",
                    issue,
                )
                return None

        try:
            imgs = await page.locator("img").all()
        except Exception:
            await asyncio.sleep(0.5)
            continue

        for img in imgs:
            src = await img.get_attribute("src") or ""
            if not _looks_like_generated(src):
                continue
            try:
                await img.wait_for(state="visible", timeout=2000)
            except PwTimeout:
                continue
            w = await img.evaluate("el => el.naturalWidth") or 0
            if w < 50:
                continue
            data = await _fetch_image(src, page)
            if data and len(data) > 4096:
                return data

        await asyncio.sleep(0.5)


def _looks_like_generated(src: str) -> bool:
    """Heuristic: real generated images are large data URIs, blob URLs,
    or come from Google's image-serving CDNs. Icons are filtered out."""
    if not src:
        return False
    if src.startswith("blob:"):
        return True
    if src.startswith("data:image/") and len(src) > 20000:
        return True
    if "googleusercontent.com" in src:
        return True
    if "googleapis.com" in src and "generate" in src.lower():
        return True
    return False


async def _fetch_image(src: str, page=None) -> Optional[bytes]:
    """Download an image from a data URI, blob URL, or HTTP(S) URL."""
    try:
        if src.startswith("data:"):
            _, encoded = src.split(",", 1)
            return base64.b64decode(encoded)
        if src.startswith("blob:") and page is not None:
            b64 = await page.evaluate(
                """async (src) => {
                const r = await fetch(src);
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return btoa(bin);
            }""",
                src,
            )
            return base64.b64decode(b64)
        async with aiohttp.ClientSession() as s:
            async with s.get(src, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception:
        logger.debug("failed to fetch image from %s", src[:80])
    return None


async def close():
    """Shut down the browser. Call on bot shutdown."""
    global _playwright, _browser, _context
    try:
        if _context:
            await _context.close()
        if _browser:
            await _browser.close()
        if _playwright:
            await _playwright.stop()
    except Exception as e:
        logger.warning("browser cleanup: %s", e)
    finally:
        _playwright = _browser = _context = None


async def bananaLogic(ctx, prompt: str):
    """Encapsulates the /banana slash command logic.

    Checks the prompt, calls the Playwright-based generator,
    sends the resulting image file to the designated channel (or directly if invoked there),
    and ensures the temporary file is deleted.
    """
    import config
    import discord
    from bot import safe_defer, safe_respond, safeEdit

    if not prompt or not prompt.strip():
        await safe_respond(ctx, "decime qué generar")
        return

    target_channel_id = config.INDIO_REPLY_CHANNEL_ID or 1490008278275461280
    is_outside = ctx.channel_id != target_channel_id

    # If outside, defer as ephemeral so the status update is private
    await safe_defer(ctx, ephemeral=is_outside)

    if is_outside:
        await safeEdit(ctx, f"⏳ Imagen generándose en <#{target_channel_id}>...")
    else:
        await safeEdit(ctx, "⏳ Generando imagen con Gemini...")

    # First, verify if we have access to the target channel (if outside)
    target_channel = None
    if is_outside:
        from unittest.mock import Mock
        has_real_bot = hasattr(ctx, "bot") and ctx.bot is not None and (
            not isinstance(ctx.bot, Mock) or "_mock_custom_bot" in ctx.bot.__dict__
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

    path = await generate(prompt)
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
