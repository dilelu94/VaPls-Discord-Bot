"""Behavioral tests for the text-only DM-based Headbanz / adivinador command.

Fakes Discord gateways, DMs, the GoLive HTTP relay, Gemini AI, and database MMR logging.
Verifies text interactions ("si"/"no" challenge, DM guessing), playing against the Indio AI userbot,
and single public victory channel announcements.
"""

import asyncio
import os
import json
import pytest
import discord
from unittest.mock import AsyncMock, MagicMock

import config
import adivinadorCommand
from adivinadorCommand import (
    start_headbanz_game,
    ensure_font_downloaded,
    generate_headbanz_image,
    handle_dm_guess,
    _pending_challenges,
    _active_games,
    INDIO_USER_ID,
)
from tests.conftest import sent_text


@pytest.fixture(autouse=True)
def mock_pillow_and_font(monkeypatch):
    """Fixture to mock font downloading and Pillow image saving to avoid file creations or downloads."""
    async def mock_download():
        pass
    async def mock_generate(u1, c1, s1, url1, u2, c2, s2, url2, path):
        with open(path, "w") as f:
            f.write("fake-composite-image")

    monkeypatch.setattr(adivinadorCommand, "ensure_font_downloaded", mock_download)
    monkeypatch.setattr(adivinadorCommand, "generate_headbanz_image", mock_generate)
    _pending_challenges.clear()
    _active_games.clear()
    yield
    _pending_challenges.clear()
    _active_games.clear()


async def test_adivinador_voice_validation_requester_outside(ctx_factory):
    ctx = ctx_factory(in_voice=False)
    opponent = MagicMock(spec=discord.Member)
    opponent.display_name = "Opponent"
    opponent.id = 2

    await start_headbanz_game(ctx, ctx.author, opponent)
    assert "tiene que estar en un canal de voz" in sent_text(ctx)


async def test_adivinador_challenge_dm_sent_ephemerally(ctx_factory, monkeypatch):
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)
    opponent = MagicMock(spec=discord.Member)
    opponent.display_name = "Opponent"
    opponent.name = "opponent"
    opponent.id = 2
    opponent.send = AsyncMock()

    await start_headbanz_game(ctx, ctx.author, opponent)

    # 1. Opponent received challenge DM
    assert opponent.send.call_count == 1
    _, kwargs = opponent.send.call_args
    assert "Desafío de Headbanz" in kwargs["embed"].title
    assert "¿Te la bancás o no te da?" in kwargs["embed"].description

    # 2. Requester received ephemeral confirmation
    assert "Desafío enviado a" in sent_text(ctx)

    # 3. Pending challenge recorded
    assert 2 in _pending_challenges


async def test_adivinador_challenge_reject_by_text_dm(ctx_factory, monkeypatch):
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)

    p1 = ctx.author
    p1.display_name = "Player 1"
    p1.name = "player1"
    p1.id = 1
    p1.send = AsyncMock()

    p2 = MagicMock(spec=discord.Member)
    p2.display_name = "Player 2"
    p2.name = "player2"
    p2.id = 2
    p2.send = AsyncMock()

    await start_headbanz_game(ctx, p1, p2)
    assert 2 in _pending_challenges

    # Player 2 sends DM "no"
    msg_reject = MagicMock(spec=discord.Message)
    msg_reject.author = p2
    msg_reject.guild = None
    msg_reject.content = "no"
    msg_reject.channel = MagicMock()
    msg_reject.channel.send = AsyncMock()

    handled = await handle_dm_guess(msg_reject)
    assert handled is True

    # Player 2 receives DM confirmation
    assert msg_reject.channel.send.call_count == 1
    assert "Rechazaste el desafío" in msg_reject.channel.send.call_args[0][0]

    # Player 1 notified by DM
    assert p1.send.call_count == 1
    assert "no se la bancó y rechazó" in p1.send.call_args[0][0]

    # Challenge cleared and NO active game
    assert 2 not in _pending_challenges
    assert 1 not in _active_games


async def test_adivinador_challenge_indio_auto_accept_and_victory(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)
    monkeypatch.setattr(config, "GOLIVE_RELAY_URL", "http://127.0.0.1:8082")
    monkeypatch.setattr(config, "GOLIVE_RELAY_SECRET", "secret")

    # Mock HTTP client for GoLive relay POST
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, url, json, headers, **kwargs):
            resp = MagicMock()
            resp.status = 200
            async def text():
                return "ok"
            resp.text = text
            async def aenter(*args, **kwargs):
                return resp
            async def aexit(*args, **kwargs):
                pass
            resp.__aenter__ = aenter
            resp.__aexit__ = aexit
            return resp

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: FakeSession())

    # Mock characters list
    chars_file = tmp_path / "adivinador_characters.json"
    chars_data = [
        {"name": "Goku", "source": "Dragon Ball Z", "image_url": ""},
        {"name": "Luffy", "source": "One Piece", "image_url": ""},
    ]
    with open(chars_file, "w", encoding="utf-8") as f:
        json.dump(chars_data, f)
    monkeypatch.setattr(adivinadorCommand, "CHARS_FILE", str(chars_file))

    # Mock stop_golive_stream and _log_activity
    stop_called = []
    async def fake_stop(gid):
        stop_called.append(gid)
        return True
    monkeypatch.setattr(adivinadorCommand, "stop_golive_stream", fake_stop)

    mmr_logs = []
    async def fake_log(user_id, guild_id, activity_type, *, quality_score=None, display_name=""):
        mmr_logs.append({
            "user_id": user_id,
            "guild_id": guild_id,
            "activity_type": activity_type,
            "quality_score": quality_score
        })
    import bot
    monkeypatch.setattr(bot, "_log_activity", fake_log)

    # Set up players (Player 1 vs Indio AI)
    p1 = ctx.author
    p1.display_name = "Player 1"
    p1.name = "player1"
    p1.id = 1
    p1.send = AsyncMock()

    indio = MagicMock(spec=discord.Member)
    indio.display_name = "Indio"
    indio.name = "indio"
    indio.id = INDIO_USER_ID
    indio.voice = MagicMock()
    indio.voice.channel = MagicMock()
    indio.voice.channel.id = 99
    indio.send = AsyncMock()

    # Mock Gemini AI generation
    import geminiClient
    async def fake_gemini_generate(prompt):
        res = MagicMock()
        res.text = "GUESS: Luffy"
        return res
    monkeypatch.setattr(geminiClient, "generate", fake_gemini_generate)

    # Start game against Indio -> auto accept
    await start_headbanz_game(ctx, p1, indio)

    # 1. Verify auto-acceptance response sent to challenger
    assert "El Indio aceptó tu desafío" in sent_text(ctx)

    # 2. Active game created with is_vs_indio=True
    assert 1 in _active_games
    assert INDIO_USER_ID in _active_games
    session = _active_games[1]
    assert session.is_vs_indio is True

    # 3. Simulate Indio AI turn winning victory
    text_channel_mock = MagicMock()
    text_channel_mock.send = AsyncMock()
    bot.bot.get_channel = MagicMock(return_value=text_channel_mock)

    await adivinadorCommand._run_indio_ai_turn(session)

    # Verify public victory announcement for Indio using standard template
    assert text_channel_mock.send.call_count == 1
    pub_msg = text_channel_mock.send.call_args[0][0]
    assert "Indio" in pub_msg
    assert "le ganó a" in pub_msg
    assert "Player 1" in pub_msg
    assert "/adivinador" in pub_msg

    # Verify MMR logged for Indio (winner) and Player 1 (loser)
    assert len(mmr_logs) == 2
    assert mmr_logs[0]["user_id"] == INDIO_USER_ID
    assert mmr_logs[0]["activity_type"] == "game_win"
    assert mmr_logs[0]["quality_score"] == 1.0
