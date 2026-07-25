"""Behavioral tests for the /adivinador command and adivinadorCommand module.

Fakes Discord gateways, DMs, the GoLive HTTP relay, and the database MMR logging.
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
    HeadbanzControlView,
)


@pytest.fixture(autouse=True)
def mock_pillow_and_font(monkeypatch):
    """Fixture to mock font downloading and Pillow image saving to avoid file creations or downloads."""
    async def mock_download():
        pass
    async def mock_generate(u1, c1, s1, url1, u2, c2, s2, url2, path):
        # Create an empty file to simulate output image creation
        with open(path, "w") as f:
            f.write("fake-composite-image")

    monkeypatch.setattr(adivinadorCommand, "ensure_font_downloaded", mock_download)
    monkeypatch.setattr(adivinadorCommand, "generate_headbanz_image", mock_generate)


def joined_messages(ctx) -> str:
    """Helper to concatenate all text messages sent through ctx.sent_messages or history."""
    msgs = []
    for m in ctx.sent_messages:
        if m is not None:
            msgs.append(m)
    for h in ctx.deferred_history:
        if h is not None:
            msgs.append(h)
    return "\n".join(msgs)


async def test_adivinador_voice_validation_requester_outside(ctx_factory):
    ctx = ctx_factory(in_voice=False)
    opponent = MagicMock(spec=discord.Member)
    opponent.display_name = "Opponent"
    opponent.id = 2

    # Execute command
    await start_headbanz_game(ctx, ctx.author, opponent)
    text = joined_messages(ctx)
    assert "tiene que estar en un canal de voz" in text


async def test_adivinador_voice_validation_opponent_outside(ctx_factory):
    # Requester in voice channel 99, opponent has no voice state
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)
    opponent = MagicMock(spec=discord.Member)
    opponent.display_name = "Opponent"
    opponent.id = 2
    opponent.voice = None

    await start_headbanz_game(ctx, ctx.author, opponent)
    text = joined_messages(ctx)
    assert "conectados al mismo canal de voz" in text or "tiene que estar en un canal de voz" in text


async def test_adivinador_voice_validation_different_channels(ctx_factory):
    # Requester in channel 99, opponent in channel 101
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)
    opponent = MagicMock(spec=discord.Member)
    opponent.display_name = "Opponent"
    opponent.id = 2
    opponent.voice = MagicMock()
    opponent.voice.channel = MagicMock()
    opponent.voice.channel.id = 101
    opponent.voice.channel.name = "Otro Canal"

    await start_headbanz_game(ctx, ctx.author, opponent)
    text = joined_messages(ctx)
    assert "mismo canal de voz" in text


async def test_adivinador_success_flow(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory(in_voice=True, voice_channel_id=99)
    monkeypatch.setattr(config, "GOLIVE_RELAY_URL", "http://127.0.0.1:8082")
    monkeypatch.setattr(config, "GOLIVE_RELAY_SECRET", "secret")

    # Mock opponent
    opponent = MagicMock(spec=discord.Member)
    opponent.display_name = "Opponent"
    opponent.id = 2
    opponent.voice = MagicMock()
    opponent.voice.channel = MagicMock()
    opponent.voice.channel.id = 99
    opponent.voice.channel.name = "Mi Canal"
    opponent.send = AsyncMock()

    # Mock requester DM
    ctx.author.send = AsyncMock()

    # Mock characters list
    chars_file = tmp_path / "adivinador_characters.json"
    chars_data = [
        {"name": "Goku", "source": "Dragon Ball Z", "image_url": ""},
        {"name": "Luffy", "source": "One Piece", "image_url": ""},
    ]
    with open(chars_file, "w", encoding="utf-8") as f:
        json.dump(chars_data, f)
    monkeypatch.setattr(adivinadorCommand, "CHARS_FILE", str(chars_file))

    # Mock HTTP client for GoLive relay POST
    post_calls = []
    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, url, json, headers, **kwargs):
            post_calls.append({"url": url, "json": json, "headers": headers})
            # Return a fake response with status 200
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

    # Execute
    await start_headbanz_game(ctx, ctx.author, opponent)

    # 1. Verify DMs were sent with crossed characters
    # Player 1 (author) must receive Opponent's character (which is randomly chosen from Luffy or Goku)
    assert ctx.author.send.call_count == 1
    assert opponent.send.call_count == 1
    
    # 2. Verify relay POST call
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "http://127.0.0.1:8082/headbanz"
    assert post_calls[0]["json"]["guild_id"] == ctx.guild.id
    assert post_calls[0]["json"]["channel_id"] == 99

    # 3. Verify interaction respond / followup view
    assert ctx.followup.send.call_count == 1
    _, kwargs = ctx.followup.send.call_args
    assert "Juego Headbanz Iniciado!" in kwargs["embed"].title
    assert isinstance(kwargs["view"], HeadbanzControlView)


async def test_adivinador_view_interactions_p1_win(monkeypatch, tmp_path):
    p1 = MagicMock(spec=discord.Member)
    p1.display_name = "Player 1"
    p1.id = 1
    p1.mention = "<@1>"
    p2 = MagicMock(spec=discord.Member)
    p2.display_name = "Player 2"
    p2.id = 2
    p2.mention = "<@2>"

    temp_img = tmp_path / "headbanz_100.png"
    temp_img.write_text("test")

    view = HeadbanzControlView(guild_id=100, player1=p1, player2=p2, image_path=str(temp_img))

    # Mock stop stream
    stop_stream_called = []
    async def fake_stop(guild_id):
        stop_stream_called.append(guild_id)
        return True
    monkeypatch.setattr(adivinadorCommand, "stop_golive_stream", fake_stop)

    # Mock _log_activity
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

    # Mock interaction
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = p1
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    # Trigger P1 win
    await view.on_p1_win(interaction)

    # Assertions
    assert interaction.response.defer.call_count == 1
    assert len(stop_stream_called) == 1
    assert stop_stream_called[0] == 100
    assert not os.path.exists(temp_img)  # Deleted temp file

    # Assert MMR logged: P1 (winner) gets game_win (q=1.0), P2 gets game_lose (q=0.0)
    assert len(mmr_logs) == 2
    assert mmr_logs[0]["user_id"] == 1
    assert mmr_logs[0]["activity_type"] == "game_win"
    assert mmr_logs[0]["quality_score"] == 1.0

    assert mmr_logs[1]["user_id"] == 2
    assert mmr_logs[1]["activity_type"] == "game_lose"
    assert mmr_logs[1]["quality_score"] == 0.0

    # Assert embed winner text is sent
    _, edit_kwargs = interaction.edit_original_response.call_args
    assert "¡Partida Finalizada!" in edit_kwargs["embed"].title
    assert "<@1>" in edit_kwargs["embed"].description


async def test_adivinador_database_integration(tmp_path):
    import userbot.activity_db as adb

    db_file = tmp_path / "activity.db"
    adb.init_db(str(db_file))

    # Log game win for User 1
    delta_win = adb.log_activity(user_id=1, guild_id=100, activity_type="game_win", quality_score=1.0, display_name="User 1")
    # Log game lose for User 2
    delta_lose = adb.log_activity(user_id=2, guild_id=100, activity_type="game_lose", quality_score=0.0, display_name="User 2")

    assert delta_win > 0
    assert delta_lose < 0

    # Retrieve user stats
    stats1 = adb.get_user_stats(user_id=1, guild_id=100)
    stats2 = adb.get_user_stats(user_id=2, guild_id=100)

    assert stats1["game_wins"] == 1
    assert stats1["game_losses"] == 0
    assert stats2["game_wins"] == 0
    assert stats2["game_losses"] == 1
