"""Catalog of images the Indio has collected from DM submissions.

The manager owns ``indio_images/manifest.json`` + the actual image files and
provides helpers to save new entries, look them up by id, and produce the
catalog block injected into the Indio's system prompt.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bot.image")


class ImageManager:
    """Thread-safe (single-threaded async) manager for the image collection.

    All I/O happens synchronously — callers are responsible for holding the
    asyncio lock if concurrent writes are possible (currently only the DM
    session uses this, which is serialised per user).
    """

    def __init__(self, images_dir: str):
        self.dir = Path(images_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.images: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, encoding="utf-8") as f:
                    data = json.load(f)
                self.images = data.get("images", [])
            except Exception as exc:
                logger.exception("image manifest load failed: %s", exc)
                self.images = []
        else:
            self.images = []
            self._save()

    def _save(self) -> None:
        tmp = self.manifest_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"images": self.images}, f, indent=2, ensure_ascii=False)
            tmp.replace(self.manifest_path)
        except Exception as exc:
            logger.exception("image manifest save failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_image(
        self,
        file_bytes: bytes,
        ext: str,
        description: str,
        tags: list[str],
        author_id: int,
        original_filename: str,
        gemini_description: str = "",
    ) -> str:
        """Persist an image + its metadata. Returns the new image id."""
        img_id = str(uuid.uuid4())
        filename = f"{img_id}.{ext.lstrip('.').lower()}"
        (self.dir / filename).write_bytes(file_bytes)
        entry = {
            "id": img_id,
            "filename": filename,
            "original_filename": original_filename,
            "description": description,
            "gemini_description": gemini_description,
            "tags": tags,
            "author_id": author_id,
            "created_at": int(time.time()),
        }
        self.images.append(entry)
        self._save()
        return img_id

    def get_image_path(self, image_id: str) -> Optional[Path]:
        for img in self.images:
            if img["id"] == image_id:
                p = self.dir / img["filename"]
                return p if p.exists() else None
        return None

    def get_image_entry(self, image_id: str) -> Optional[dict]:
        for img in self.images:
            if img["id"] == image_id:
                return dict(img)
        return None

    def total_images(self) -> int:
        return len(self.images)

    def get_catalog_block(self) -> str:
        """Format the image collection for injection into the Indio's system prompt.

        Returns an empty string when there are no images so the caller can skip
        the block entirely.
        """
        if not self.images:
            return ""
        lines = [
            "\n[IMÁGENES DISPONIBLES] Tenés una colección de imágenes del grupo. "
            "Cuando tenga sentido mostrarlas (chiste visual, momento gracioso, "
            "referencia), usá la herramienta ``use_image`` con el ID correspondiente.\n"
        ]
        for img in self.images[-50:]:
            tags = ", ".join(img["tags"][:5])
            lines.append(f"- ID: {img['id']} | {img['description']} | tags: {tags}")
        return "\n".join(lines)
