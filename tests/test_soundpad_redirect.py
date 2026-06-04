"""Behavior: when /soundpad is invoked from a channel other than
INDIO_PLAY_CHANNEL_ID, the panel and query-mode clip confirmation are
posted in the redirect_channel instead of via ctx.followup.
When no redirect_channel is supplied the output stays on ctx.followup."""

import os
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soundpadCommand import soundpadLogic


def _fake_redirect_channel():
    """Minimal fake text channel that records sent messages."""
    sent: list[str] = []

    async def _send(content=None, **kwargs):
        sent.append(content)
        return types.SimpleNamespace(id=9000, channel=types.SimpleNamespace(id=999))

    ch = MagicMock(name="RedirectChannel")
    ch.id = 999
    ch.send = AsyncMock(side_effect=_send)
    ch.sent_messages = sent
    return ch


def _make_ctx(tmp_path, ctx_factory):
    """Build a ctx with a temporary audio dir that has at least one category+clip."""
    cat = tmp_path / "Efectos"
    cat.mkdir()
    (cat / "boom.mp3").write_bytes(b"dummy")

    ctx = ctx_factory(guild_id=100, in_voice=True)
    ctx.guild.voice_client = None
    ctx.guild.get_channel = MagicMock(return_value=None)

    # geminiKeys gate: let everyone through
    import geminiKeys
    ctx._geminiKeys_patched = True
    return ctx, str(tmp_path)


async def test_soundpad_panel_goes_to_redirect_channel(
    ctx_factory, tmp_path, monkeypatch
):
    """When redirect_channel is provided, the panel is posted there instead of
    ctx.followup."""
    import geminiKeys
    import config

    monkeypatch.setattr(geminiKeys, "has_user_key", lambda uid: True)
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))

    cat = tmp_path / "Clips"
    cat.mkdir()
    (cat / "test.mp3").write_bytes(b"dummy")

    ctx = ctx_factory(guild_id=100, in_voice=True)
    ctx.guild.voice_client = None

    # stub voice connect so soundpadLogic doesn't hang
    voice_channel = types.SimpleNamespace(id=99)
    ctx.author.voice.channel = voice_channel

    async def _fake_connect(**kwargs):
        return MagicMock(is_connected=lambda: True, is_playing=lambda: False)

    voice_channel.connect = _fake_connect

    redirect_ch = _fake_redirect_channel()

    # Block music-vote gate
    import playCommand
    monkeypatch.setattr(playCommand, "get_active_vote", lambda gid: None)

    await soundpadLogic(ctx, query=None, redirect_channel=redirect_ch)

    # Panel landed in redirect channel
    assert any(
        "Soundpad" in (m or "") for m in redirect_ch.sent_messages
    ), f"expected panel in redirect channel, got: {redirect_ch.sent_messages}"
    # Not in ctx.followup
    assert all(
        "Soundpad" not in (m or "") for m in ctx.sent_messages
    ), f"panel should not be in ctx.followup, got: {ctx.sent_messages}"


async def test_soundpad_panel_uses_followup_without_redirect(
    ctx_factory, tmp_path, monkeypatch
):
    """Without redirect_channel, the panel goes through ctx.followup as usual."""
    import geminiKeys
    import config

    monkeypatch.setattr(geminiKeys, "has_user_key", lambda uid: True)
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))

    cat = tmp_path / "Clips"
    cat.mkdir()
    (cat / "test.mp3").write_bytes(b"dummy")

    ctx = ctx_factory(guild_id=100, in_voice=True)
    ctx.guild.voice_client = None

    voice_channel = types.SimpleNamespace(id=99)
    ctx.author.voice.channel = voice_channel

    async def _fake_connect(**kwargs):
        return MagicMock(is_connected=lambda: True, is_playing=lambda: False)

    voice_channel.connect = _fake_connect

    import playCommand
    monkeypatch.setattr(playCommand, "get_active_vote", lambda gid: None)

    await soundpadLogic(ctx, query=None, redirect_channel=None)

    assert any(
        "Soundpad" in (m or "") for m in ctx.sent_messages
    ), f"expected panel via ctx.followup, got: {ctx.sent_messages}"


async def test_soundpad_query_confirmation_goes_to_redirect_channel(
    ctx_factory, tmp_path, monkeypatch
):
    """In query mode, the '▶️ Reproduciendo' message goes to redirect_channel."""
    import geminiKeys
    import config
    import soundpadCommand

    monkeypatch.setattr(geminiKeys, "has_user_key", lambda uid: True)
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))

    cat = tmp_path / "Clips"
    cat.mkdir()
    clip = cat / "explosion.mp3"
    clip.write_bytes(b"dummy")

    ctx = ctx_factory(guild_id=100, in_voice=True)
    ctx.guild.voice_client = None

    voice_channel = types.SimpleNamespace(id=99)
    ctx.author.voice.channel = voice_channel

    # Stub play_clip_by_query so it doesn't try to actually connect to voice
    monkeypatch.setattr(
        soundpadCommand,
        "play_clip_by_query",
        AsyncMock(return_value=str(clip)),
    )

    import playCommand
    monkeypatch.setattr(playCommand, "get_active_vote", lambda gid: None)
    monkeypatch.setattr(playCommand, "guildPlayers", {})

    redirect_ch = _fake_redirect_channel()

    await soundpadLogic(ctx, query="explosion", redirect_channel=redirect_ch)

    assert any(
        "Reproduciendo" in (m or "") for m in redirect_ch.sent_messages
    ), f"expected clip confirmation in redirect channel: {redirect_ch.sent_messages}"
    assert all(
        "Reproduciendo" not in (m or "") for m in ctx.sent_messages
    ), f"clip confirmation should not be in ctx.followup: {ctx.sent_messages}"


async def test_soundpad_query_confirmation_uses_followup_without_redirect(
    ctx_factory, tmp_path, monkeypatch
):
    """Without redirect, the query-mode confirmation stays in ctx.followup."""
    import geminiKeys
    import config
    import soundpadCommand

    monkeypatch.setattr(geminiKeys, "has_user_key", lambda uid: True)
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(tmp_path))

    cat = tmp_path / "Clips"
    cat.mkdir()
    clip = cat / "explosion.mp3"
    clip.write_bytes(b"dummy")

    ctx = ctx_factory(guild_id=100, in_voice=True)
    ctx.guild.voice_client = None

    voice_channel = types.SimpleNamespace(id=99)
    ctx.author.voice.channel = voice_channel

    monkeypatch.setattr(
        soundpadCommand,
        "play_clip_by_query",
        AsyncMock(return_value=str(clip)),
    )

    import playCommand
    monkeypatch.setattr(playCommand, "get_active_vote", lambda gid: None)
    monkeypatch.setattr(playCommand, "guildPlayers", {})

    await soundpadLogic(ctx, query="explosion", redirect_channel=None)

    assert any(
        "Reproduciendo" in (m or "") for m in ctx.sent_messages
    ), f"expected confirmation via ctx.followup: {ctx.sent_messages}"
