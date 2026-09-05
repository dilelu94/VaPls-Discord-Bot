"""Behavioral tests for Stremio Web UI static routing, HTTP API endpoints, and slash commands."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import apiServer


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.guilds = []
    return bot


@pytest.mark.asyncio
async def test_stremio_static_and_api_endpoints(mock_bot):
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. GET /stremio serves index.html
        resp = await client.get("/stremio")
        assert resp.status == 200
        text = await resp.text()
        assert "VaPls Stremio" in text

        # 2. GET /stremio/style.css serves static CSS
        resp_css = await client.get("/stremio/style.css")
        assert resp_css.status == 200
        css_text = await resp_css.text()
        assert "--primary-purple" in css_text

        # 3. GET /api/stremio/search
        with patch("torrent_search.search_stremio_catalog", new=AsyncMock(return_value=[{"id": "kitsu:11", "title": "Naruto", "type": "anime"}])):
            resp_search = await client.get("/api/stremio/search?q=Naruto&type=anime")
            assert resp_search.status == 200
            search_json = await resp_search.json()
            assert len(search_json) == 1
            assert search_json[0]["title"] == "Naruto"

        # 4. GET /api/stremio/meta
        with patch("torrent_search.get_stremio_meta", new=AsyncMock(return_value={"id": "kitsu:11", "title": "Naruto", "episodes": []})):
            resp_meta = await client.get("/api/stremio/meta?id=kitsu:11&type=anime")
            assert resp_meta.status == 200
            meta_json = await resp_meta.json()
            assert meta_json["title"] == "Naruto"

        # 5. GET /api/stremio/streams
        with patch("torrent_search.get_stremio_streams", new=AsyncMock(return_value=[{"title": "Naruto Ep 1 1080p", "url": "https://torrentio.strem.fun/resolve/torbox/1/2"}])):
            resp_streams = await client.get("/api/stremio/streams?id=kitsu:11&type=anime&season=1&episode=1")
            assert resp_streams.status == 200
            streams_json = await resp_streams.json()
            assert len(streams_json) == 1
            assert "1080p" in streams_json[0]["title"]

        # 6. GET /api/stremio/voice-channels
        guild_mock = MagicMock()
        guild_mock.id = 123
        guild_mock.name = "Test Server"
        ch_mock = MagicMock()
        ch_mock.id = 456
        ch_mock.name = "General Voice"
        member_mock = MagicMock()
        member_mock.bot = False
        member_mock.id = 999
        ch_mock.members = [member_mock]
        guild_mock.voice_channels = [ch_mock]
        mock_bot.guilds = [guild_mock]

        resp_vc = await client.get("/api/stremio/voice-channels")
        assert resp_vc.status == 200
        vc_json = await resp_vc.json()
        assert len(vc_json["channels"]) == 1
        assert vc_json["channels"][0]["id"] == "456"

        # 7. POST /api/stremio/play (relays to GOLIVE_RELAY_URL)
        mock_relay_resp = MagicMock()
        mock_relay_resp.status = 200
        mock_relay_resp.json = AsyncMock(return_value={"status": "ok", "message": "Streaming started"})
        mock_relay_resp.__aenter__.return_value = mock_relay_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_relay_resp):
            resp_play = await client.post(
                "/api/stremio/play",
                json={"channel_id": "456", "url": "https://torrentio.strem.fun/resolve/torbox/1/2", "title": "Naruto"},
            )
            assert resp_play.status == 200
            play_json = await resp_play.json()
            assert play_json["status"] == "ok"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_slash_commands():
    from bot import stream

    ctx = AsyncMock()
    ctx.guild = MagicMock()
    ctx.guild_id = 123
    ctx.author.voice.channel.id = 456
    ctx.channel_id = 789

    # Test /stream stremio command
    with patch("bot.safe_defer", new=AsyncMock()):
        await stream(ctx, canal="stremio")
        assert ctx.interaction.edit_original_response.called
        kwargs = ctx.interaction.edit_original_response.call_args[1]
        embed = kwargs["embed"]
        assert "Stremio & Anime" in embed.title

    # Test /stream anime command
    with patch("bot.safe_defer", new=AsyncMock()):
        await stream(ctx, canal="anime")
        assert ctx.interaction.edit_original_response.called
        kwargs = ctx.interaction.edit_original_response.call_args[1]
        embed = kwargs["embed"]
        assert "Stremio & Anime" in embed.title


