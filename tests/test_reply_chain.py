import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
userbot_dir = os.path.join(repo_root, "userbot")
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if userbot_dir not in sys.path:
    sys.path.append(userbot_dir)

if "discord.ext.voice_recv" not in sys.modules:
    sys.modules["discord.ext.voice_recv"] = MagicMock()

if "faster_whisper" not in sys.modules:
    sys.modules["faster_whisper"] = MagicMock()

import pytest
import config
from userbot.bot import _extract_reply_chain, _extract_media_from_message


def _make_msg(
    content: str,
    author_id: int,
    author_name: str,
    ref_msg=None,
    attachments=None,
    snapshots=None,
    embeds=None,
):
    msg = MagicMock()
    msg.content = content
    msg.author = SimpleNamespace(id=author_id, display_name=author_name, bot=False)
    msg.attachments = attachments or []
    msg.message_snapshots = snapshots or []
    msg.embeds = embeds or []
    if ref_msg:
        msg.reference = SimpleNamespace(message_id=999)
        msg.referenced_message = ref_msg
    else:
        msg.reference = None
        msg.referenced_message = None
    return msg


@pytest.mark.asyncio
async def test_extract_reply_chain_single_reply():
    """Single level reply returns single parent message content and author."""
    parent = _make_msg("Hola mundo", 101, "Mati")
    current = _make_msg("@Indio respondiendo", 202, "Viny", ref_msg=parent)

    content, author, is_indio, media = await _extract_reply_chain(current)

    assert content == "Hola mundo"
    assert author == "Mati"
    assert is_indio is False
    assert media is None


@pytest.mark.asyncio
async def test_extract_reply_chain_nested_replies():
    """Nested replies (A -> B -> C) format the full ancestor chain."""
    msg_a = _make_msg("vamos a jugar", 101, "Mati")
    msg_b = _make_msg("no puedo bro", 102, "Viny", ref_msg=msg_a)
    msg_c = _make_msg("@Indio decile algo", 103, "Fran", ref_msg=msg_b)

    content, author, is_indio, media = await _extract_reply_chain(msg_c)

    assert author == "Viny"  # Direct parent's author
    assert "Mati: vamos a jugar" in content
    assert "Viny (respondiendo a Mati): no puedo bro" in content
    assert is_indio is False


@pytest.mark.asyncio
async def test_extract_reply_chain_indio_in_ancestors(monkeypatch):
    """is_reply_to_indio is True if any ancestor was authored by Indio/VaPls bot."""
    indio_id = 999111
    monkeypatch.setattr(config, "VAPLS_BOT_ID", indio_id, raising=False)

    msg_a = _make_msg("Que opinan?", indio_id, "Indio")
    msg_b = _make_msg("yo opino que no", 102, "Viny", ref_msg=msg_a)
    msg_c = _make_msg("coincido", 103, "Fran", ref_msg=msg_b)

    content, author, is_indio, media = await _extract_reply_chain(msg_c)

    assert is_indio is True
    assert author == "Viny"


@pytest.mark.asyncio
async def test_extract_reply_chain_respects_max_depth():
    """Chain traversal stops at max_depth levels."""
    msg_a = _make_msg("Nivel 1", 101, "User1")
    msg_b = _make_msg("Nivel 2", 102, "User2", ref_msg=msg_a)
    msg_c = _make_msg("Nivel 3", 103, "User3", ref_msg=msg_b)
    msg_d = _make_msg("Nivel 4", 104, "User4", ref_msg=msg_c)
    msg_e = _make_msg("Nivel 5", 105, "User5", ref_msg=msg_d)

    content, author, is_indio, media = await _extract_reply_chain(msg_e, max_depth=2)

    assert "User4" in author or "User4" in content
    assert "User1" not in content  # Truncated by max_depth


def test_extract_media_from_message_forwarded_snapshot():
    """Forwarded message snapshot attachments are extracted properly."""
    attach = MagicMock()
    attach.url = "https://cdn.discordapp.com/attachments/1/2/photo.jpg"
    attach.content_type = "image/jpeg"
    attach.filename = "photo.jpg"

    snap = MagicMock()
    snap.message = MagicMock()
    snap.message.attachments = [attach]

    msg = _make_msg("Mirá esto", 101, "User1", snapshots=[snap])

    media = _extract_media_from_message(msg)
    assert media is not None
    assert len(media) == 1
    assert media[0]["url"] == "https://cdn.discordapp.com/attachments/1/2/photo.jpg"
    assert media[0]["mime_type"] == "image/jpeg"


def test_extract_media_from_message_content_url():
    """Image URLs in message content are extracted as fallback media."""
    msg = _make_msg(
        "Miren esta foto https://cdn.discordapp.com/attachments/123/456/meme.png",
        101,
        "User1",
    )

    media = _extract_media_from_message(msg)
    assert media is not None
    assert len(media) == 1
    assert media[0]["url"] == "https://cdn.discordapp.com/attachments/123/456/meme.png"
    assert media[0]["mime_type"] == "image/png"
