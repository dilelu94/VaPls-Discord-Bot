"""Pool of unsaved images for the Indio story system.

Manages ``indio_images/pool/`` with random selection and dedup against the
``imageManager.ImageManager`` manifest (so a previously-saved image never
gets picked again).
"""

import asyncio
import logging
import os
import random
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
    """Return set of original_filename and filename values from the manifest."""
    used = set()
    for img in mgr.images:
        orig = img.get("original_filename")
        if orig:
            used.add(orig)
            used.add(Path(orig).name)
        fname = img.get("filename")
        if fname:
            used.add(fname)
            used.add(Path(fname).name)
    return used


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


async def init_pool() -> int:
    """Initialise the image pool.

    Scans the pool directory and caches the file list.

    Safe to call multiple times — scans only once.

    Returns the number of available images in the pool.
    """
    global _pool_images, _pool_initialized
    async with _pool_init_lock:
        Path(POOL_DIR).mkdir(parents=True, exist_ok=True)
        _pool_images = _scan_pool_dir()
        _pool_initialized = True
        logger.info("pool initialised with %d images", len(_pool_images))
        return len(_pool_images)


def get_random_image(
    mgr: imageManager.ImageManager,
    exclude_paths: Optional[set[str]] = None,
) -> Optional[dict]:
    """Pick a random unprocessed image strictly from the pool.

    Excludes images that are already saved in the manifest or are currently
    in active review / pending approval.

    Args:
        mgr: The ImageManager whose manifest is checked for dedup.
        exclude_paths: Optional set of relative paths to exclude (e.g. active reviews).

    Returns:
        A dict with ``rel_path``, ``subfolder``, ``filename``, or ``None``
        when the unprocessed pool is empty or all images are excluded.
    """
    global _pool_images
    Path(POOL_DIR).mkdir(parents=True, exist_ok=True)
    _pool_images = _scan_pool_dir()

    used = _manifest_original_filenames(mgr)
    excluded = set(exclude_paths or [])

    candidates = [
        e for e in _pool_images
        if e["rel_path"] not in used
        and e["filename"] not in used
        and e["rel_path"] not in excluded
        and e["filename"] not in excluded
    ]

    if not candidates:
        logger.warning("no unprocessed images available in pool")
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
