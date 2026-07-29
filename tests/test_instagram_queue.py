import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import discord

import config
import instagramCommand
from instagramCommand import (
    start_instagram_reel_stream_logic,
    _instagram_reel_queues,
    _current_golive_stream_types,
)
from bot import on_voice_state_update


@pytest.fixture(autouse=True)
def setup_golive_config():
    """Setup dummy GoLive relay configurations for testing."""
    old_url = config.GOLIVE_RELAY_URL
    old_secret = config.GOLIVE_RELAY_SECRET
    config.GOLIVE_RELAY_URL = "http://localhost:8082"
    config.GOLIVE_RELAY_SECRET = "test_secret"
    yield
    config.GOLIVE_RELAY_URL = old_url
    config.GOLIVE_RELAY_SECRET = old_secret


@pytest.fixture(autouse=True)
def clean_instagram_queues():
    """Clear queues and active streams before/after each test."""
    _instagram_reel_queues.clear()
    _current_golive_stream_types.clear()
    yield
    _instagram_reel_queues.clear()
    _current_golive_stream_types.clear()


class MockResponse:
    def __init__(self, status, text_val=""):
        self.status = status
        self._text_val = text_val

    async def text(self):
        return self._text_val

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_reel_stream_logic_success():
    """A successful GoLive stream POST sets the active type to 'reel'."""
    guild_id = 12345
    mock_vc = MagicMock(spec=discord.VoiceChannel)
    mock_vc.id = 67890

    mock_post = MagicMock(return_value=MockResponse(200, "OK"))

    with patch("aiohttp.ClientSession.post", mock_post):
        success, msg = await start_instagram_reel_stream_logic(
            guild_id, mock_vc, "https://instagram.com/reel/123", sender_name="mati"
        )
        assert success is True
        assert msg.startswith("playing:")
        assert _current_golive_stream_types.get(guild_id) == "reel"


@pytest.mark.asyncio
async def test_reel_stream_logic_queueing():
    """If a Reel is already playing (HTTP 409), the new one gets queued."""
    guild_id = 12345
    mock_vc = MagicMock(spec=discord.VoiceChannel)
    mock_vc.id = 67890

    # Simulate active Reel stream
    _current_golive_stream_types[guild_id] = "reel"

    mock_post = MagicMock(return_value=MockResponse(409, "busy"))

    with patch("aiohttp.ClientSession.post", mock_post):
        success, msg = await start_instagram_reel_stream_logic(
            guild_id, mock_vc, "https://instagram.com/reel/abc", sender_name="tobi"
        )
        assert success is True
        assert msg == "queued:1"
        assert len(_instagram_reel_queues[guild_id]) == 1
        assert _instagram_reel_queues[guild_id][0]["url"] == "https://instagram.com/reel/abc"
        assert _instagram_reel_queues[guild_id][0]["sender_name"] == "tobi"


@pytest.mark.asyncio
async def test_reel_stream_logic_busy_tv():
    """If an IPTV stream is playing (HTTP 409), queueing is skipped."""
    guild_id = 12345
    mock_vc = MagicMock(spec=discord.VoiceChannel)
    mock_vc.id = 67890

    # Simulate active IPTV stream
    _current_golive_stream_types[guild_id] = "iptv"

    mock_post = MagicMock(return_value=MockResponse(409, "busy"))

    with patch("aiohttp.ClientSession.post", mock_post):
        success, msg = await start_instagram_reel_stream_logic(
            guild_id, mock_vc, "https://instagram.com/reel/xyz", sender_name="tobi"
        )
        assert success is False
        assert msg == "busy"
        assert guild_id not in _instagram_reel_queues


@pytest.mark.asyncio
async def test_on_voice_state_update_dispatch():
    """When GoLive bot stops streaming, the next Reel in queue is automatically played."""
    guild_id = 12345
    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = guild_id

    mock_vc = MagicMock(spec=discord.VoiceChannel)
    mock_vc.id = 67890
    mock_vc.guild = mock_guild

    mock_member = MagicMock(spec=discord.Member)
    mock_member.id = config.GOLIVE_USER_ID
    mock_member.guild = mock_guild

    mock_before = MagicMock(spec=discord.VoiceState)
    mock_before.self_stream = True
    mock_before.channel = mock_vc

    mock_after = MagicMock(spec=discord.VoiceState)
    mock_after.self_stream = False
    mock_after.channel = mock_vc

    # Queue a Reel
    _instagram_reel_queues[guild_id] = [
        {"url": "https://instagram.com/reel/queued1", "sender_name": "mati", "voice_channel": mock_vc}
    ]
    _current_golive_stream_types[guild_id] = "reel"

    mock_play_logic = AsyncMock(return_value=(True, "playing:..."))
    mock_text_channel = MagicMock()
    mock_text_channel.send = AsyncMock()

    with patch("bot.bot.get_channel", return_value=mock_text_channel), \
         patch("instagramCommand.start_instagram_reel_stream_logic", mock_play_logic):
        
        await on_voice_state_update(mock_member, mock_before, mock_after)
        
        # Verify active state was cleared on teardown
        assert _current_golive_stream_types.get(guild_id) is None
        
        # Verify the item was popped from the queue
        assert guild_id not in _instagram_reel_queues
        
        # Check text channel alert was sent
        mock_text_channel.send.assert_called_once()
        
        # Wait a brief moment to allow the delayed playback task to run
        await asyncio.sleep(2.0)
        
        # Check that start_instagram_reel_stream_logic was triggered
        mock_play_logic.assert_called_once_with(
            guild_id, mock_vc, "https://instagram.com/reel/queued1", sender_name="mati"
        )
