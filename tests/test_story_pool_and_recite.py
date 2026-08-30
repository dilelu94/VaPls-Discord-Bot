"""Behavioral tests for story image pool fallback, directory creation, channel fallback, and TTS recitation."""

import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config as bot_config
import imageManager
import imagePool
import storyManager


@pytest.fixture
def temp_pool_and_catalog(tmp_path, monkeypatch):
    pool_dir = tmp_path / "pool"
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setattr(imagePool, "POOL_DIR", str(pool_dir), raising=False)
    monkeypatch.setattr(bot_config, "INDIO_IMAGES_DIR", str(catalog_dir), raising=False)
    return pool_dir, catalog_dir


async def test_init_pool_creates_directory_if_missing(temp_pool_and_catalog):
    pool_dir, _ = temp_pool_and_catalog
    assert not pool_dir.exists()

    count = await imagePool.init_pool()

    assert pool_dir.exists()
    assert count == 0


def test_get_random_image_strict_pool_only(temp_pool_and_catalog):
    pool_dir, catalog_dir = temp_pool_and_catalog
    pool_dir.mkdir(parents=True, exist_ok=True)
    catalog_dir.mkdir(parents=True, exist_ok=True)

    # Create a saved image in catalog
    saved_file = catalog_dir / "saved_123.jpg"
    saved_file.write_bytes(b"saved-content")

    mgr = imageManager.ImageManager(str(catalog_dir))
    mgr.images.append({
        "id": "123",
        "filename": "saved_123.jpg",
        "original_filename": "orig.jpg",
        "description": "Saved Image",
        "tags": ["test"],
        "author_id": 0,
        "created_at": 1000,
    })

    # Pool is empty — must return None (no fallback to catalog)
    pick = imagePool.get_random_image(mgr)
    assert pick is None

    # Now add an unprocessed image to pool
    (pool_dir / "unprocessed.jpg").write_bytes(b"unprocessed")
    pick = imagePool.get_random_image(mgr)
    assert pick is not None
    assert pick["filename"] == "unprocessed.jpg"

    # If unprocessed.jpg is in exclude_paths, return None
    pick_excluded = imagePool.get_random_image(mgr, exclude_paths={"unprocessed.jpg"})
    assert pick_excluded is None


async def test_post_review_spawns_tts_recitation(temp_pool_and_catalog, monkeypatch):
    pool_dir, _ = temp_pool_and_catalog
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "test_story.jpg").write_bytes(b"test-bytes")

    bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot.get_channel.return_value = channel

    with patch.object(storyManager, "_relay_payload", AsyncMock(return_value=12345)), \
         patch("geminiCommand._speak_indio_reply", AsyncMock()) as mock_speak:
        ok = await storyManager._post_review(
            channel_id=999,
            rel_path="test_story.jpg",
            story_text="Un chiste muy gracioso sobre la foto.",
            guild_id=123,
            bot=bot,
        )

        assert ok is True
        mock_speak.assert_called_once_with(
            bot, 123, None, "Un chiste muy gracioso sobre la foto.", max_chars=600
        )


async def test_post_review_channel_fallback_on_migration(temp_pool_and_catalog, monkeypatch):
    pool_dir, _ = temp_pool_and_catalog
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "test_story.jpg").write_bytes(b"test-bytes")

    bot = MagicMock()
    # Primary channel lookup fails
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=Exception("Channel not found"))

    # Guild fallback channel
    fallback_channel = MagicMock()
    fallback_channel.id = 555
    fallback_channel.send = AsyncMock()

    guild = MagicMock()
    guild.get_channel.side_effect = lambda cid: fallback_channel if cid == bot_config.INDIO_REPLY_CHANNEL_ID else None
    bot.get_guild.return_value = guild

    with patch.object(storyManager, "_relay_payload", AsyncMock(return_value=12345)), \
         patch("geminiCommand._speak_indio_reply", AsyncMock()):
        ok = await storyManager._post_review(
            channel_id=999,  # Missing channel ID
            rel_path="test_story.jpg",
            story_text="Chiste con fallback.",
            guild_id=777,
            bot=bot,
        )

        assert ok is True
