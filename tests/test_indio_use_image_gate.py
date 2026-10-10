"""Behavioral tests for the Indio use_image gate and multimodal tool filtering."""

from __future__ import annotations

import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import geminiCommand
from geminiCommand import (
    _build_indio_system_instruction,
    _gate_use_image_actions,
    indioFromVoice,
)


# ---------------------------------------------------------------------------
# 1. Deterministic gate: _gate_use_image_actions
# ---------------------------------------------------------------------------


def test_gate_use_image_drops_when_attachments_present():
    """When the user provided media/attachments, USE_IMAGE is always suppressed."""
    actions = [("USE_IMAGE", '{"image_id": "bb773572", "caption": ""}')]
    # Even with an imperative order, attachments mean user is showing media
    res = _gate_use_image_actions(actions, "indio mostrá la foto", has_attachments=True)
    assert len(res) == 0


def test_gate_use_image_drops_on_opinion_questions():
    """Questions asking for opinions/reactions ('qué opinás', 'qué pensás', 'mirá esto') suppress USE_IMAGE."""
    actions = [("USE_IMAGE", '{"image_id": "bb773572", "caption": ""}')]

    for phrase in [
        "indio que opinas",
        "indio qué opinás",
        "que opinas de esto",
        "indio que pensas",
        "mira esto",
        "que te parece",
        "como ves esto",
        "indio que onda",
    ]:
        res = _gate_use_image_actions(actions, phrase, has_attachments=False)
        assert len(res) == 0, f"Failed for phrase: {phrase}"


def test_gate_use_image_drops_on_unrelated_chat():
    """General conversation without any show/send image command suppresses USE_IMAGE."""
    actions = [("USE_IMAGE", '{"image_id": "bb773572", "caption": ""}')]
    res = _gate_use_image_actions(actions, "hola indio como andas todo bien", has_attachments=False)
    assert len(res) == 0


def test_gate_use_image_keeps_on_explicit_order():
    """Explicit requests to show/send a photo or meme keep USE_IMAGE."""
    actions = [("USE_IMAGE", '{"image_id": "bb773572", "caption": "mirá"}')]

    for phrase in [
        "indio mostrá la foto de juji",
        "mostrame la imagen de juji",
        "mandame una foto de viny",
        "pasá la foto de juji",
        "tirate una foto de juji",
        "poné la imagen de juji",
        "compartí la foto de milardo",
    ]:
        res = _gate_use_image_actions(actions, phrase, has_attachments=False)
        assert len(res) == 1, f"Failed to keep for phrase: {phrase}"
        assert res[0][0] == "USE_IMAGE"


def test_gate_use_image_preserves_other_actions():
    """Actions other than USE_IMAGE (PLAY_SOUND, PLAY_MUSIC, etc.) are untouched."""
    actions = [
        ("PLAY_SOUND", "cuello"),
        ("USE_IMAGE", "bb773572"),
        ("SAVE_MEMORY", "algo"),
    ]
    res = _gate_use_image_actions(actions, "indio que opinas", has_attachments=False)
    assert ("PLAY_SOUND", "cuello") in res
    assert ("SAVE_MEMORY", "algo") in res
    assert not any(a == "USE_IMAGE" for a, _ in res)


# ---------------------------------------------------------------------------
# 2. System Instruction catalog injection control
# ---------------------------------------------------------------------------


def test_build_indio_system_instruction_excludes_catalog_when_disabled(monkeypatch):
    """When include_images=False, the catalog block [IMÁGENES DISPONIBLES] is omitted."""
    instruction = _build_indio_system_instruction(include_images=False)
    assert "[IMÁGENES DISPONIBLES]" not in instruction


def test_build_indio_system_instruction_includes_catalog_by_default():
    """By default (include_images=True), the catalog block is injected if images exist."""
    instruction = _build_indio_system_instruction()
    # The repo has images in indio_images/
    assert "[IMÁGENES DISPONIBLES]" in instruction


# ---------------------------------------------------------------------------
# 3. Behavioral test: indioFromVoice multimodal path
# ---------------------------------------------------------------------------


def _make_bot_and_channel(guild_id=100, channel_id=111):
    channel = MagicMock(name=f"Chan({channel_id})")
    channel.id = channel_id
    channel.send = AsyncMock(return_value=types.SimpleNamespace(id=7777))

    guild = MagicMock()
    guild.id = guild_id
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Seba", name="seba")
    )
    guild.get_channel = MagicMock(return_value=channel)
    guild.text_channels = []

    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]
    return bot, channel


@pytest.mark.asyncio
async def test_indioFromVoice_with_image_attachment_strips_use_image_tool(
    indio, patch_generate, reply_factory, monkeypatch
):
    """When attachment_urls includes an image, use_image is stripped from tools and catalog omitted."""
    import aiohttp
    import config

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    captured_call = {}

    async def _mock_generate(**kwargs):
        captured_call.update(kwargs)
        return reply_factory(text="Ese perro tiene tremendo cuello boludo")

    monkeypatch.setattr("geminiClient.generate", _mock_generate)

    # Fake download of attachment
    class _FakeResponse:
        status = 200
        async def read(self):
            return b"fake-png-data"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class _FakeSession:
        def get(self, url, **kwargs):
            return _FakeResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)

    bot, channel = _make_bot_and_channel()
    attachments = [
        {"url": "https://cdn.discordapp.com/attachments/123/perro.png", "mime_type": "image/png"}
    ]

    await indioFromVoice(
        bot,
        user_id=42,
        guild_id=100,
        channel_id=111,
        pregunta="indio que opinas",
        speaker_name="Seba",
        attachment_urls=attachments,
    )

    # Assert tools provided to Gemini do NOT include use_image
    tools = captured_call.get("tools") or []
    tool_names = [t.get("name") for t in tools]
    assert "use_image" not in tool_names

    # Assert system_instruction does NOT include [IMÁGENES DISPONIBLES]
    system_inst = captured_call.get("system_instruction") or ""
    assert "[IMÁGENES DISPONIBLES]" not in system_inst

    # Assert image_parts was passed to Gemini
    image_parts = captured_call.get("image_parts")
    assert image_parts is not None
    assert len(image_parts) == 1

    # Assert response text reached the channel
    assert channel.send.called
    sent_text = channel.send.call_args[0][0]
    assert "cuello" in sent_text


@pytest.mark.asyncio
async def test_dispatch_use_image_falls_back_to_non_empty_caption(monkeypatch):
    """When USE_IMAGE runs with empty caption and empty reply_text, caption is never empty."""
    import config

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    bot, channel = _make_bot_and_channel()

    # Dispatch USE_IMAGE with empty caption
    statuses = await geminiCommand._dispatch_indio_actions(
        bot=bot,
        guild_id=100,
        actions=[("USE_IMAGE", '{"image_id": "bb773572-8cc9-41fc-9e56-52814abb9eb6", "caption": ""}')],
        reply_handle=types.SimpleNamespace(channel_id=111),
        reply_text="",
    )

    assert len(statuses) == 1
    assert "use_image: ok" in statuses[0]
    assert channel.send.called
    kwargs = channel.send.call_args.kwargs
    # Content sent with the image MUST not be empty
    assert kwargs.get("content")
    assert "file" in kwargs


@pytest.mark.asyncio
async def test_indioLogic_suppressed_tool_falls_back_to_friendly_text(
    indio, ctx_factory, patch_generate, reply_factory
):
    """If the model only emits use_image and it gets gated out, indioLogic falls back to friendly text."""
    patch_generate(
        reply=reply_factory(
            text="",
            function_calls=[{"name": "use_image", "args": {"image_id": "bb773572"}}],
        )
    )
    ctx = ctx_factory(guild_id=100)
    await geminiCommand.indioLogic(ctx, "indio que opinas", nuevo=False)

    assert ctx.followup.send.called
    sent_msg = ctx.followup.send.call_args[0][0]
    assert sent_msg  # Not empty
    assert "No entendí" in sent_msg or "¿Qué onda?" in sent_msg

