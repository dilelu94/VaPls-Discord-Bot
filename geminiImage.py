"""Image generation via Gemini web UI using Playwright browser automation.

Free alternative to the paid Imagen/3.1 Flash Image API. Uses a persisted
Gmail session (storage_state) to access gemini.google.com's native image
generation without an API key.

One-time setup::
    python setup_gemini_session.py   # log in manually, saves auth file

Then the bot loads ``gemini_auth.json`` at startup and reuses it for every
``/generarimagen`` command.
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
        ],
    )
    if os.path.exists(storage_path):
        _context = await _browser.new_context(storage_state=storage_path)
        logger.info("browser ready (session from %s)", storage_path)
        return True

    logger.warning(
        "no auth file at %s — run setup_gemini_session.py first", storage_path
    )
    _context = await _browser.new_context()
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
        await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        ta = page.locator("textarea").first
        await ta.wait_for(state="visible", timeout=15000)
        await ta.fill(prompt)
        await ta.press("Enter")

        img_data = await _wait_for_image(page)
        if img_data is None:
            return None
        if len(img_data) > MAX_FILE_SIZE:
            logger.warning("image %d bytes exceeds 8 MB limit", len(img_data))
            return None

        fd, path = tempfile.mkstemp(suffix=".png", prefix="gimg_")
        try:
            os.write(fd, img_data)
        finally:
            os.close(fd)
        return path
    except Exception:
        logger.exception("image generation failed")
        return None
    finally:
        await page.close()


async def _wait_for_image(page, timeout: int = GENERATE_TIMEOUT) -> Optional[bytes]:
    """Poll the page until a generated image appears and return its bytes."""
    from playwright.async_api import TimeoutError as PwTimeout

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            logger.warning("timeout waiting for generated image")
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
            w = await img.get_attribute("naturalWidth") or "0"
            if not w.isdigit() or int(w) < 50:
                continue
            data = await _fetch_image(src)
            if data and len(data) > 4096:
                return data

        await asyncio.sleep(0.5)


def _looks_like_generated(src: str) -> bool:
    """Heuristic: real generated images are large data URIs or come from
    Google's image-serving CDNs. Icons and avatars are filtered out."""
    if not src:
        return False
    if src.startswith("data:image/") and len(src) > 20000:
        return True
    if "googleusercontent.com" in src:
        return True
    if "googleapis.com" in src and "generate" in src.lower():
        return True
    return False


async def _fetch_image(src: str) -> Optional[bytes]:
    """Download an image from a data URI or HTTP(S) URL."""
    try:
        if src.startswith("data:"):
            _, encoded = src.split(",", 1)
            return base64.b64decode(encoded)
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
