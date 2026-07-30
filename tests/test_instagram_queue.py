import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch

# Mock local modules not installed in dev/test environments so golive.bot imports successfully
for _mod in ("video_compat", "davey_compat", "golive_connection", "instagram_feed", "instagram_streamer", "streamer", "ytdlp"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import discord
if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

# golive/bot.py imports the root config.py but expects golive/config.py LOG_LEVEL
import config
config.LOG_LEVEL = "INFO"

# Now we can import golive.bot directly from the package structure
import golive.bot as golive_bot
from golive.bot import GoLiveStream, _relay_stream, _active_streams

import pytest
import asyncio
from aiohttp import web


@pytest.fixture(autouse=True)
def clean_active_streams():
    """Clear active streams dictionary before/after each test."""
    _active_streams.clear()
    yield
    _active_streams.clear()


@pytest.mark.asyncio
async def test_golive_stream_initial_state():
    """Constructor sets up queue, queue_titles, and is_first_reel flag correctly."""
    mock_vc = MagicMock()
    mock_vc.ssrc = 100

    # Instagram Reel URL
    stream1 = GoLiveStream(None, 123, 456, mock_vc, "https://instagram.com/reel/123")
    assert stream1.queue == []
    assert stream1.queue_titles == []
    assert stream1.is_first_reel is True

    # IPTV URL (Non-Instagram)
    stream2 = GoLiveStream(None, 123, 456, mock_vc, "http://iptv.com/channel.m3u8")
    assert stream2.is_first_reel is False


@pytest.mark.asyncio
async def test_golive_stream_first_reel_delay():
    """First Reel stream connect applies a 5-second initial connect sleep."""
    mock_vc = MagicMock()
    mock_vc.ssrc = 100

    stream = GoLiveStream(None, 123, 456, mock_vc, "https://instagram.com/reel/123")
    
    mock_connect = AsyncMock()
    mock_start_players = AsyncMock()
    mock_sleep = AsyncMock()
    mock_extract = AsyncMock(return_value=("https://instagram.com/reel/123", "Reel Title", False))

    ytdlp_module = sys.modules.get('ytdlp')

    with patch("golive.bot.GoLiveConnection") as mock_golive_conn_class, \
         patch.object(stream, "_start_players", mock_start_players), \
         patch.object(ytdlp_module, "_yt_extract_url", mock_extract), \
         patch("asyncio.sleep", mock_sleep), \
         patch("asyncio.create_task") as mock_create_task:

        mock_conn_inst = MagicMock()
        mock_conn_inst.connect = mock_connect
        mock_conn_inst.ssrc = 100
        mock_golive_conn_class.return_value = mock_conn_inst

        await stream.start()
        
        # Verify 5 second connect delay was applied
        mock_sleep.assert_called_once_with(5.0)
        assert stream.is_first_reel is False


@pytest.mark.asyncio
async def test_relay_stream_queueing():
    guild_id = 123
    mock_vc = MagicMock()
    mock_vc.ssrc = 100

    active_stream = GoLiveStream(None, guild_id, 456, mock_vc, "https://instagram.com/reel/current")
    _active_streams[guild_id] = active_stream

    # Construct dummy HTTP Request
    mock_request = MagicMock(spec=web.Request)
    mock_request.remote = "127.0.0.1"
    
    # Auth headers setup using root config mocks
    with patch("golive.bot.config") as mock_golive_config:
        mock_golive_config.RELAY_SECRET = "test"
        mock_request.headers = {"X-API-Secret": "test"}

        # Request payload
        payload = {
            "guild_id": guild_id,
            "channel_id": 456,
            "url": "https://instagram.com/reel/new_one",
            "channel_name": "Mati Reel"
        }
        mock_request.json = AsyncMock(return_value=payload)

        # Trigger relay_stream
        with patch("golive.bot.client.is_ready", return_value=True):
            resp = await _relay_stream(mock_request)
            assert resp.status == 200
            
            # Check queue
            assert len(active_stream.queue) == 1
            assert active_stream.queue[0] == "https://instagram.com/reel/new_one"
            assert active_stream.queue_titles[0] == "Mati Reel"


@pytest.mark.asyncio
async def test_inactivity_loop_consumes_queue():
    """GoLiveStream inactivity loop plays next Reel in queue on natural video end."""
    guild_id = 123
    mock_vc = MagicMock()
    mock_vc.ssrc = 100

    stream = GoLiveStream(None, guild_id, 456, mock_vc, "https://instagram.com/reel/1")
    stream.queue = ["https://instagram.com/reel/2"]
    stream.queue_titles = ["Second Reel"]
    stream.is_live = False  # VOD

    # Simulate player ended
    mock_player = MagicMock()
    mock_player.is_alive = MagicMock(return_value=False)
    stream.video_player = mock_player

    mock_stop = AsyncMock()
    mock_start = AsyncMock()
    mock_extract = AsyncMock(return_value=("http://stream_url", "Title", False))
    mock_client = MagicMock()
    mock_client.get_guild = MagicMock(return_value=None)

    # Configure the mocked ytdlp module
    ytdlp_module = sys.modules.get('ytdlp')
    ytdlp_module._yt_extract_url = mock_extract

    sleep_calls = 0
    async def mock_sleep_impl(sec):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    with patch.object(stream, "_stop_players", mock_stop), \
         patch.object(stream, "_start_players", mock_start), \
         patch("golive.bot.client", mock_client):
         
         # Run the inactivity loop with the mock sleep
         with patch("asyncio.sleep", mock_sleep_impl):
             try:
                 await stream._inactivity_loop()
             except asyncio.CancelledError:
                 pass
             
             # Verify stop was called to clean up old players
             mock_stop.assert_called_once()
             
             # Verify next url extraction
             mock_extract.assert_called_once_with("https://instagram.com/reel/2")
             
             # Verify it updated current URL and started new players
             assert stream.url == "https://instagram.com/reel/2"
             mock_start.assert_called_once()
             assert stream.queue == []
