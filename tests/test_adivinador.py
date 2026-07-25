"""Behavioral tests for the redesigned DM-based Headbanz / adivinador command.

Fakes Discord gateways, DMs, the GoLive HTTP relay, and the database MMR logging.
Verifies strict DM-only interactions and single public victory channel announcement.
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
    HeadbanzChallengeView,
    handle_dm_guess,
    _active_games,
)


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
    _active_games.clear()
    yield
    _active_games.clear()


from tests.conftest import sent_text


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
    assert isinstance(kwargs["view"], HeadbanzChallengeView)

    # 2. Requester received ephemeral confirmation
    assert "Desafío enviado a" in sent_text(ctx)


async def test_adivinador_challenge_reject_flow(monkeypatch):
    p1 = MagicMock(spec=discord.Member)
    p1.display_name = "Player 1"
    p1.name = "player1"
    p1.id = 1
    p1.send = AsyncMock()

    p2 = MagicMock(spec=discord.Member)
    p2.display_name = "Player 2"
    p2.name = "player2"
    p2.id = 2

    view = HeadbanzChallengeView(guild_id=100, text_channel_id=42, player1=p1, player2=p2)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    # Trigger rejection ("No me da 🐔")
    await view.on_reject(interaction)

    # 1. Player 2 DM edited with rejection
    assert interaction.edit_original_response.call_count == 1
    _, kwargs = interaction.edit_original_response.call_args
    assert "Rechazaste el desafío" in kwargs["content"]

    # 2. Player 1 notified by DM
    assert p1.send.call_count == 1
    p1_dm_text = p1.send.call_args[0][0]
    assert "no se la bancó y rechazó" in p1_dm_text


async def test_adivinador_challenge_accept_and_guess_flow(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)
    monkeypatch.setattr(config, "GOLIVE_RELAY_URL", "http://127.0.0.1:8082")
    monkeypatch.setattr(config, "GOLIVE_RELAY_SECRET", "secret")

    # Mock HTTP client for GoLive relay POST
    post_calls = []
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, url, json, headers, **kwargs):
            post_calls.append({"url": url, "json": json, "headers": headers})
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

    # Set up players
    p1 = ctx.author
    p1.display_name = "Player 1"
    p1.name = "player1"
    p1.id = 1
    p1.send = AsyncMock()

    p2 = MagicMock(spec=discord.Member)
    p2.display_name = "Player 2"
    p2.name = "player2"
    p2.id = 2
    p2.voice = MagicMock()
    p2.voice.channel = MagicMock()
    p2.voice.channel.id = 99
    p2.send = AsyncMock()

    view = HeadbanzChallengeView(guild_id=ctx.guild.id, text_channel_id=ctx.channel_id, player1=p1, player2=p2)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    # 1. Player 2 accepts challenge ("¡Me la banco! ⚔️")
    await view.on_accept(interaction)

    # Verify DMs sent with crossed characters
    assert p1.send.call_count == 2  # Accept notice + Crossed card DM
    assert p2.send.call_count == 1  # Crossed card DM

    # Verify game session created
    assert 1 in _active_games
    assert 2 in _active_games
    session = _active_games[1]

    # Verify NO public channel message sent during setup
    assert ctx.followup.send.call_count == 0

    # 2. Player 1 sends INCORRECT DM guess
    msg_incorrect = MagicMock(spec=discord.Message)
    msg_incorrect.author = p1
    msg_incorrect.guild = None
    msg_incorrect.content = "Naruto"
    msg_incorrect.channel = MagicMock()
    msg_incorrect.channel.send = AsyncMock()

    handled = await handle_dm_guess(msg_incorrect)
    assert handled is True
    assert msg_incorrect.channel.send.call_count == 1
    assert "no es tu personaje" in msg_incorrect.channel.send.call_args[0][0]

    # Active session still exists
    assert 1 in _active_games

    # 3. Player 1 sends CORRECT DM guess for their assigned character (session.char1['name'])
    correct_char_name = session.char1["name"]
    msg_correct = MagicMock(spec=discord.Message)
    msg_correct.author = p1
    msg_correct.guild = None
    msg_correct.content = correct_char_name
    msg_correct.channel = MagicMock()
    msg_correct.channel.send = AsyncMock()

    # Mock bot.get_channel for the SINGLE public victory announcement
    text_channel_mock = MagicMock()
    text_channel_mock.send = AsyncMock()
    bot.bot.get_channel = MagicMock(return_value=text_channel_mock)

    handled_correct = await handle_dm_guess(msg_correct)
    assert handled_correct is True

    # Verify winner DM sent to guesser
    assert msg_correct.channel.send.call_count == 1
    assert "¡CORRECTO!" in msg_correct.channel.send.call_args[0][0]

    # VERIFY SINGLE PUBLIC ANNOUNCEMENT IN SERVER TEXT CHANNEL
    assert text_channel_mock.send.call_count == 1
    pub_msg = text_channel_mock.send.call_args[0][0]
    assert "le ganó a" in pub_msg
    assert "/adivinador" in pub_msg

    # Verify MMR logged: P1 (winner) gets 1.0, P2 gets 0.0
    assert len(mmr_logs) == 2
    assert mmr_logs[0]["user_id"] == 1
    assert mmr_logs[0]["activity_type"] == "game_win"
    assert mmr_logs[0]["quality_score"] == 1.0

    assert mmr_logs[1]["user_id"] == 2
    assert mmr_logs[1]["activity_type"] == "game_lose"
    assert mmr_logs[1]["quality_score"] == 0.0

    # Verify stream stopped and session cleared
    assert len(stop_called) == 1
    assert 1 not in _active_games
    assert 2 not in _active_games
