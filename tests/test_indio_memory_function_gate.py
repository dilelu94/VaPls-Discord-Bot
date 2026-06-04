"""Behavior: turns that trigger function calls (play_music, play_sound, etc.)
are NOT saved to ``_indio_history`` in any entry point (slash, wake-word text,
voice). Only purely conversational turns persist — function-calling turns are
operational and would pollute the model's context on future calls.

All entry points share the same memory key (per-guild), so the gate is tested
on both ``indioLogic`` and ``indioFromVoice``."""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from geminiCommand import indioFromVoice, indioLogic

KEY = "guild-100"


def history(gc, key=KEY):
    return gc._indio_history.get(key, [])


def texts(turns):
    return [p["text"] for t in turns for p in t["parts"]]


async def _drain():
    current = asyncio.current_task()
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# indioLogic (slash command)
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_indioLogic_function_call_skips_history(
    indio, ctx_factory, patch_generate, reply_factory
):
    """Slash: mensaje que dispara play_music no se guarda en memoria."""
    patch_generate(
        reply=reply_factory(
            text="dale, va",
            function_calls=[{"name": "play_music", "args": {"query": "Queen"}}],
        )
    )
    await indioLogic(ctx_factory(guild_id=100), "pone Queen", nuevo=False)
    await _drain()

    assert len(history(indio)) == 0


async def test_indioLogic_normal_chat_saves_history(
    indio, ctx_factory, patch_generate, reply_factory
):
    """Slash: mensaje normal (sin funcion) se guarda en memoria."""
    patch_generate(reply=reply_factory(text="todo bien"))
    await indioLogic(ctx_factory(guild_id=100), "como andas", nuevo=False)

    assert len(history(indio)) == 2


@pytest.mark.slow
async def test_indioLogic_function_then_normal(
    indio, ctx_factory, patch_generate, reply_factory
):
    """Slash: primero una funcion (no persiste), despues charla normal (si)."""
    import playCommand

    patch_generate(
        reply=reply_factory(
            text="dale",
            function_calls=[{"name": "play_music", "args": {"query": "Queen"}}],
        )
    )
    await indioLogic(ctx_factory(guild_id=100), "pone Queen", nuevo=False)
    await _drain()
    assert len(history(indio)) == 0

    # Cerrar la votacion abierta en el primer turno para que el segundo
    # turno pueda pasar el gate de "no hay votacion activa".
    v = playCommand.get_active_vote(100)
    if v is not None:
        v._closed = True
        if v:
            v._cancel_timers()

    patch_generate(reply=reply_factory(text="bien y vos"))
    await indioLogic(ctx_factory(guild_id=100), "bien", nuevo=False)

    assert len(history(indio)) == 2
    assert any("bien" in t for t in texts(history(indio)))


# ---------------------------------------------------------------------------
# indioFromVoice text path (from_voice=False)
# ---------------------------------------------------------------------------


def _make_bot_and_channel(guild_id=100, channel_id=111):
    """Build a minimal fake bot with one channel for indioFromVoice tests."""
    channel = MagicMock(name=f"Chan({channel_id})")
    channel.id = channel_id
    channel.send = AsyncMock(return_value=types.SimpleNamespace(id=7777))

    guild = MagicMock()
    guild.id = guild_id
    guild.emojis = []
    guild.get_member = MagicMock(
        return_value=types.SimpleNamespace(id=42, display_name="Tobi", name="tobi")
    )
    guild.get_channel = MagicMock(return_value=channel)
    guild.text_channels = []

    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_guild = MagicMock(return_value=guild)
    bot.guilds = [guild]
    return bot, channel


@pytest.mark.slow
async def test_indioFromVoice_text_function_call_skips_history(
    indio, patch_generate, reply_factory, monkeypatch
):
    """Wake-word texto: mensaje que dispara play_music no se guarda."""
    import config

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    patch_generate(
        reply=reply_factory(
            text="dale, va",
            function_calls=[{"name": "play_music", "args": {"query": "Queen"}}],
        )
    )

    bot, _ = _make_bot_and_channel()
    await indioFromVoice(
        bot,
        user_id=42,
        guild_id=100,
        channel_id=111,
        pregunta="pone Queen",
        speaker_name="Tobi",
    )
    await _drain()

    assert len(history(indio)) == 0


async def test_indioFromVoice_text_normal_saves_history(
    indio, patch_generate, reply_factory, monkeypatch
):
    """Wake-word texto: mensaje normal (sin funcion) se guarda en memoria."""
    import config

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    patch_generate(reply=reply_factory(text="todo bien che"))
    bot, _ = _make_bot_and_channel()

    await indioFromVoice(
        bot,
        user_id=42,
        guild_id=100,
        channel_id=111,
        pregunta="che indio que onda",
        speaker_name="Tobi",
    )

    assert len(history(indio)) == 2


# ---------------------------------------------------------------------------
# indioFromVoice voice path (from_voice=True) — already excluded
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_indioFromVoice_voice_skips_history(
    indio, patch_generate, reply_factory, monkeypatch
):
    """Voz (from_voice=True): no se guarda nunca, tenga o no function call."""
    import config

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    # Con function call — no se guarda
    patch_generate(
        reply=reply_factory(
            text="dale",
            function_calls=[{"name": "play_music", "args": {"query": "Queen"}}],
        )
    )
    bot, _ = _make_bot_and_channel()
    await indioFromVoice(
        bot,
        user_id=42,
        guild_id=100,
        channel_id=111,
        pregunta="pone Queen",
        speaker_name="Tobi",
        from_voice=True,
    )
    await _drain()
    assert len(history(indio)) == 0


async def test_indioFromVoice_voice_no_function_still_no_history(
    indio, patch_generate, reply_factory, monkeypatch
):
    """Voz (from_voice=True): sin function call tampoco se guarda."""
    import config

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)

    patch_generate(reply=reply_factory(text="todo bien"))
    bot, _ = _make_bot_and_channel()
    await indioFromVoice(
        bot,
        user_id=42,
        guild_id=100,
        channel_id=111,
        pregunta="che indio como va",
        speaker_name="Tobi",
        from_voice=True,
    )

    assert len(history(indio)) == 0
