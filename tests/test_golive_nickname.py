import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# Mock local modules not installed in test environments so golive.bot imports cleanly
for _mod in ("video_compat", "davey_compat", "golive_connection", "golive.slopsoil.golive", "instagram_feed", "instagram_streamer", "streamer", "ytdlp"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import discord
discord.VoiceClient = MagicMock()
if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

import config
config.LOG_LEVEL = "INFO"

import golive.bot as golive_bot
from golive.bot import (
    _save_original_nickname,
    _set_nickname,
    _restore_nickname,
    _original_nicknames,
    _active_streams,
    on_voice_state_update,
    on_ready,
)


@pytest.fixture(autouse=True)
def clean_nick_state():
    """Clear nickname tracking state before and after each test."""
    _original_nicknames.clear()
    _active_streams.clear()
    yield
    _original_nicknames.clear()
    _active_streams.clear()


@pytest.mark.asyncio
async def test_save_and_restore_original_nickname():
    """_save_original_nickname stores current nick and _restore_nickname restores it immediately."""
    mock_me = MagicMock()
    mock_me.nick = "NormalUserNick"
    mock_me.edit = AsyncMock()

    mock_guild = MagicMock()
    mock_guild.id = 999
    mock_guild.me = mock_me

    _save_original_nickname(mock_guild)
    assert _original_nicknames[999] == "NormalUserNick"

    # Change nick to stream title
    await _set_nickname(mock_guild, "GoLive - Al Jazeera English")
    mock_me.edit.assert_awaited_with(nick="GoLive - Al Jazeera English")
    mock_me.nick = "GoLive - Al Jazeera English"

    # Restore nick
    await _restore_nickname(mock_guild)
    mock_me.edit.assert_awaited_with(nick="NormalUserNick")
    assert 999 not in _original_nicknames


@pytest.mark.asyncio
async def test_restore_nickname_clears_nick_when_none():
    """When original nickname was None, _restore_nickname calls me.edit(nick=None) to reset to default."""
    mock_me = MagicMock()
    mock_me.nick = None
    mock_me.edit = AsyncMock()

    mock_guild = MagicMock()
    mock_guild.id = 888
    mock_guild.me = mock_me

    _save_original_nickname(mock_guild)
    assert _original_nicknames[888] is None

    # Change nick
    mock_me.nick = "GoLive - Test"
    await _set_nickname(mock_guild, "GoLive - Test")

    # Restore nick
    await _restore_nickname(mock_guild)
    mock_me.edit.assert_awaited_with(nick=None)


@pytest.mark.asyncio
async def test_on_voice_state_update_restores_nickname_on_leave():
    """Leaving voice channel triggers immediate nickname restoration."""
    bot_id = 123
    mock_me = MagicMock()
    mock_me.nick = "GoLive - Live Stream"
    mock_me.edit = AsyncMock()

    mock_guild = MagicMock()
    mock_guild.id = 777
    mock_guild.me = mock_me

    _original_nicknames[777] = "NormalNick"

    member = MagicMock()
    member.id = bot_id

    before = MagicMock()
    before.channel = MagicMock(guild=mock_guild)

    after = MagicMock()
    after.channel = None

    mock_client = MagicMock()
    mock_client.user.id = bot_id

    with patch("golive.bot.client", mock_client), \
         patch("golive.bot._stop_idle_watchdog"):
        await on_voice_state_update(member, before, after)
        mock_me.edit.assert_awaited_with(nick="NormalNick")


@pytest.mark.asyncio
async def test_on_ready_cleans_stale_stream_nicknames():
    """on_ready resets nicknames starting with 'GoLive - ' if no stream is active."""
    bot_id = 123
    mock_me = MagicMock()
    mock_me.nick = "GoLive - Stale Title"
    mock_me.edit = AsyncMock()

    mock_guild = MagicMock()
    mock_guild.id = 555
    mock_guild.me = mock_me

    mock_client = MagicMock()
    mock_client.user = MagicMock(id=bot_id)
    mock_client.guilds = [mock_guild]

    with patch("golive.bot.client", mock_client):
        await on_ready()
        mock_me.edit.assert_awaited_with(nick=None)
