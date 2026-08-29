import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch

# Mock local modules not installed in dev/test environments so golive.bot imports successfully
for _mod in ("video_compat", "davey_compat", "golive_connection", "golive.slopsoil.golive", "instagram_feed", "instagram_streamer", "streamer", "ytdlp"):
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
    """First Reel stream connect initializes stream via slopsoil."""
    mock_vc = MagicMock()
    mock_vc.ssrc = 100
    mock_vc.move_to = AsyncMock()

    stream = GoLiveStream(None, 123, 456, mock_vc, "https://instagram.com/reel/123")
    
    mock_extract = AsyncMock(return_value=("https://instagram.com/reel/123", "Reel Title", False))
    ytdlp_module = sys.modules.get('ytdlp')

    with patch("golive.bot.slopsoil_start_live_stream", AsyncMock()), \
         patch.object(ytdlp_module, "_yt_extract_url", mock_extract):
        await stream.start()
        assert stream.target_url == "https://instagram.com/reel/123"


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
        mock_guild = MagicMock()
        mock_guild.id = guild_id
        mock_channel = MagicMock(spec=discord.VoiceChannel)
        mock_channel.name = "general"
        mock_guild.get_channel = MagicMock(return_value=mock_channel)
        ytdlp_module = sys.modules.get('ytdlp')
        with patch("golive.bot.client.is_ready", return_value=True), \
             patch("golive.bot.client.get_guild", return_value=mock_guild), \
             patch("golive.bot._guild_allowed", return_value=True), \
             patch("golive.bot._join_channel", AsyncMock()), \
             patch("golive.bot._vc_for_guild", return_value=MagicMock()), \
             patch.object(ytdlp_module, "_yt_extract_url", AsyncMock(return_value=None)), \
             patch("golive.bot.slopsoil_start_live_stream", AsyncMock()):
            resp = await _relay_stream(mock_request)
            # Slopsoil engine always starts a new stream (no queue logic)
            assert resp.status == 200


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


@pytest.mark.asyncio
async def test_prefetch_next_caches_result():
    """Prefetch resolves the queue head and stores it in the cache."""
    stream = GoLiveStream(None, 123, 456, MagicMock(), "https://instagram.com/reel/1")
    stream.queue = ["https://instagram.com/reel/2"]
    stream.queue_titles = ["Second Reel"]
    ytdlp_module = sys.modules.get('ytdlp')
    mock_extract = AsyncMock(return_value=("http://stream_url", "Title", False))
    ytdlp_module._yt_extract_url = mock_extract

    await stream._prefetch_next()

    assert stream._prefetch_cache == {"https://instagram.com/reel/2": ("http://stream_url", "Title", False)}
    mock_extract.assert_awaited_once_with("https://instagram.com/reel/2")
    assert stream._prefetch_urls == set()


@pytest.mark.asyncio
async def test_prefetch_next_inflight_guard():
    """A URL already being resolved is not extracted again."""
    stream = GoLiveStream(None, 123, 456, MagicMock(), "https://instagram.com/reel/1")
    stream.queue = ["https://instagram.com/reel/2"]
    stream.queue_titles = ["Second Reel"]
    stream._prefetch_urls.add("https://instagram.com/reel/2")
    ytdlp_module = sys.modules.get('ytdlp')
    mock_extract = AsyncMock()
    ytdlp_module._yt_extract_url = mock_extract

    await stream._prefetch_next()

    mock_extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefetch_next_cache_hit_skips_extract():
    """A URL already in the cache is not extracted again."""
    stream = GoLiveStream(None, 123, 456, MagicMock(), "https://instagram.com/reel/1")
    stream.queue = ["https://instagram.com/reel/2"]
    stream.queue_titles = ["Second Reel"]
    stream._prefetch_cache["https://instagram.com/reel/2"] = ("u", "t", False)
    ytdlp_module = sys.modules.get('ytdlp')
    mock_extract = AsyncMock()
    ytdlp_module._yt_extract_url = mock_extract

    await stream._prefetch_next()

    mock_extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactivity_loop_uses_prefetched_url():
    """Transition consumes a prefetched URL without another yt-dlp call."""
    guild_id = 123
    mock_vc = MagicMock()
    mock_vc.ssrc = 100

    stream = GoLiveStream(None, guild_id, 456, mock_vc, "https://instagram.com/reel/1")
    stream.queue = ["https://instagram.com/reel/2"]
    stream.queue_titles = ["Second Reel"]
    stream.is_live = False
    stream._prefetch_cache["https://instagram.com/reel/2"] = ("http://stream_url", "Title", False)

    mock_player = MagicMock()
    mock_player.is_alive = MagicMock(return_value=False)
    stream.video_player = mock_player

    mock_stop = AsyncMock()
    mock_start = AsyncMock()
    mock_extract = MagicMock()
    mock_client = MagicMock()
    mock_client.get_guild = MagicMock(return_value=None)

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
         patch("golive.bot.client", mock_client), \
         patch("asyncio.sleep", mock_sleep_impl):
        try:
            await stream._inactivity_loop()
        except asyncio.CancelledError:
            pass

        mock_extract.assert_not_called()
        mock_start.assert_called_once()
        assert stream.url == "https://instagram.com/reel/2"
        assert stream._prefetch_cache == {}


@pytest.mark.asyncio
async def test_stop_players_snapshots_counters():
    """Stopping players captures their final RTP seq/ts for the next reel."""
    stream = GoLiveStream(None, 123, 456, MagicMock(), "https://instagram.com/reel/1")

    mock_player = MagicMock()
    mock_player.is_alive.return_value = False
    mock_player._seq = 12345
    mock_player._ts = 2_160_000
    stream.video_player = mock_player

    mock_sender = MagicMock()
    mock_sender.is_alive.return_value = False
    mock_sender._seq = 600
    mock_sender._ts = 1_152_000
    stream.audio_sender = mock_sender

    await stream._stop_players()

    assert stream._video_seq == 12345
    assert stream._video_ts == 2_160_000
    assert stream._audio_seq == 600
    assert stream._audio_ts == 1_152_000
    assert stream.video_player is None
    assert stream.audio_sender is None


@pytest.mark.asyncio
async def test_stop_players_ignores_missing_counters():
    """Players without integer counters leave the stored state unchanged."""
    stream = GoLiveStream(None, 123, 456, MagicMock(), "https://instagram.com/reel/1")
    stream._video_seq = 10
    stream._video_ts = 20
    stream._audio_seq = 30
    stream._audio_ts = 40

    mock_player = MagicMock()
    mock_player.is_alive.return_value = False
    stream.video_player = mock_player

    mock_sender = MagicMock()
    mock_sender.is_alive.return_value = False
    stream.audio_sender = mock_sender

    await stream._stop_players()

    assert stream._video_seq == 10
    assert stream._video_ts == 20
    assert stream._audio_seq == 30
    assert stream._audio_ts == 40


@pytest.mark.asyncio
async def test_start_players_seeds_continuity():
    """Players are created with the previous reel's RTP counters as seeds."""
    stream = GoLiveStream(None, 123, 456, MagicMock(), "https://instagram.com/reel/1")
    stream.conn = MagicMock()
    stream._video_seq = 500
    stream._video_ts = 2_000_000
    stream._audio_seq = 900
    stream._audio_ts = 1_152_000

    streamer_mod = sys.modules.get('streamer')
    hp = MagicMock()
    streamer_mod.H264VideoPlayer = MagicMock(return_value=hp)
    streamer_mod._stream_fps = MagicMock(return_value=25.0)

    glc_mod = sys.modules.get('golive_connection')
    asender = MagicMock()
    glc_mod.GoLiveAudioSender = MagicMock(return_value=asender)
    glc_mod._GoLiveVCProxy = MagicMock(return_value=MagicMock())

    with patch("asyncio.wait_for", AsyncMock(return_value=MagicMock())), \
         patch("asyncio.to_thread", MagicMock(return_value=MagicMock())):
        await stream._start_players()

    vkw = streamer_mod.H264VideoPlayer.call_args.kwargs
    assert vkw["initial_seq"] == 500
    assert vkw["initial_ts"] == 2_000_000
    akw = glc_mod.GoLiveAudioSender.call_args.kwargs
    assert akw["initial_seq"] == 900
    assert akw["initial_ts"] == 1_152_000
