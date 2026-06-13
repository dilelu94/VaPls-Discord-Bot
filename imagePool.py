"""Pool of unsaved images for the Indio story system.

Extracts images from a source zip into ``indio_images/pool/``, tracks which
files are available, and provides random selection with dedup against the
``imageManager.ImageManager`` manifest (so a previously-saved image never
gets picked again).
"""

import asyncio
import json
import logging
import os
import random
import zipfile
from pathlib import Path
from typing import Optional

import imageManager
import config

logger = logging.getLogger("bot.imagePool")

POOL_DIR = "indio_images/pool"

# Cached list of available pool images: each entry is
#   {"rel_path": str, "subfolder": str, "filename": str, "basename_no_ext": str}
_pool_images: list[dict] = []
_pool_init_lock = asyncio.Lock()
_pool_initialized = False


def _manifest_original_filenames(mgr: imageManager.ImageManager) -> set[str]:
    """Return set of ``original_filename`` values from the manifest."""
    return {img.get("original_filename", "") for img in mgr.images}


def _scan_pool_dir() -> list[dict]:
    """Walk ``POOL_DIR`` and return entries for every image file found."""
    root = Path(POOL_DIR)
    if not root.exists():
        return []
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    entries: list[dict] = []
    for f in root.rglob("*"):
        if f.suffix.lower() not in IMAGE_EXTS:
            continue
        rel = f.relative_to(root)
        parts = rel.parts
        subfolder = parts[0] if len(parts) > 1 else ""
        entries.append(
            {
                "rel_path": str(rel),
                "subfolder": subfolder,
                "filename": f.name,
                "basename_no_ext": f.stem,
            }
        )
    return entries


def _find_zip() -> Optional[Path]:
    """Find the source zip, trying server path first, then config."""
    candidates = [
        "/home/ubuntu/vapls-discord-bot/transfers/d2500ca820304f5d961c54e652b3a1dd/"
        "Pibes Vapor-20260612T031328Z-3-001.zip",
        "transfers/Pibes Vapor-20260612T031328Z-3-001.zip",
    ]
    for p in candidates:
        path = Path(p)
        if path.exists():
            return path
    return None


def _extract_zip(zip_path: Path) -> int:
    """Extract only image files from the zip into ``POOL_DIR``.

    The zip has a ``Pibes Vapor/`` root folder; images inside live in
    subfolders like ``Viny/``, ``Fox/``, etc. We strip the root prefix
    so the pool dir mirrors the subfolder layout directly.

    Returns the number of images extracted.
    """
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    root = Path(POOL_DIR)
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            p = Path(name)
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            # Strip "Pibes Vapor/" prefix if present
            if len(p.parts) > 1 and p.parts[0] == "Pibes Vapor":
                rel = Path(*p.parts[1:])
            else:
                rel = p
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())
            count += 1
    logger.info("extracted %d images from %s into %s", count, zip_path, POOL_DIR)
    return count


async def init_pool() -> int:
    """Initialise the image pool.

    If ``POOL_DIR`` is empty, attempts to find and extract the source zip.
    Then scans the pool directory and caches the file list.

    Safe to call multiple times — the extract step only runs once.

    Returns the number of available images in the pool.
    """
    global _pool_images, _pool_initialized
    async with _pool_init_lock:
        if _pool_initialized:
            return len(_pool_images)
        root = Path(POOL_DIR)
        existing = list(root.rglob("*")) if root.exists() else []
        image_files = (
            [
                f
                for f in existing
                if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
            ]
            if existing
            else []
        )
        if not image_files:
            z = _find_zip()
            if z:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _extract_zip, z)
            else:
                logger.info("no zip found, pool will be empty")
        _pool_images = _scan_pool_dir()
        _pool_initialized = True
        logger.info("pool initialised with %d images", len(_pool_images))
        return len(_pool_images)


def get_random_image(mgr: imageManager.ImageManager) -> Optional[dict]:
    """Pick a random image from the pool that is not already in the manifest.

    Args:
        mgr: The ImageManager whose manifest is checked for dedup.

    Returns:
        A dict with ``rel_path``, ``subfolder``, ``filename``, or ``None``
        when the pool is exhausted.
    """
    available = [e for e in _pool_images if not Path(POOL_DIR, e["rel_path"]).exists()]
    if available:
        _pool_images[:] = available
    used = _manifest_original_filenames(mgr)
    candidates = [e for e in _pool_images if e["rel_path"] not in used]
    if not candidates:
        return None
    return random.choice(candidates)


def remove_from_pool(rel_path: str) -> bool:
    """Delete a pool image by its relative path.

    Returns True if the file was deleted, False if it didn't exist.
    """
    target = Path(POOL_DIR, rel_path)
    if not target.exists():
        return False
    target.unlink()
    return True


def is_pool_empty() -> bool:
    """Return True when there are no images left in the pool."""
    return not _pool_images
