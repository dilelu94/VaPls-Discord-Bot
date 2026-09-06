"""Behavioral tests for Stremio Web UI session token security, HTTP API endpoints, and slash commands."""

import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import apiServer
from stremio_sessions import session_manager, StremioSessionManager


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.guilds = []
    return bot


@pytest.fixture(autouse=True)
def clean_sessions():
    session_manager.sessions.clear()
    yield
    session_manager.sessions.clear()


def test_session_manager_lifecycle():
    mgr = StremioSessionManager(default_ttl_hours=1.0)
    sess = mgr.create_session(
        author_id=123,
        author_name="Tester",
        channel_id=456,
        guild_id=789,
        ttl_hours=1.0,
    )
    assert sess.token is not None
    assert len(sess.token) == 32
    assert mgr.validate_token(sess.token) is True
    assert mgr.get_session(sess.token) == sess

    # Test expired token
    sess.expires_at = time.time() - 10.0
    assert mgr.get_session(sess.token) is None
    assert mgr.validate_token(sess.token) is False

    # Test non-existent token
    assert mgr.get_session("invalid-token") is None
    assert mgr.validate_token("invalid-token") is False


@pytest.mark.asyncio
async def test_stremio_unauthenticated_access_rejected(mock_bot):
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. GET /stremio without token -> 403 Access Denied HTML
        resp = await client.get("/stremio")
        assert resp.status == 403
        text = await resp.text()
        assert "Acceso Denegado" in text
        assert "/stream stremio" in text

        # 2. GET /api/stremio/search without token -> 403 JSON
        resp_search = await client.get("/api/stremio/search?q=Naruto")
        assert resp_search.status == 403
        body_search = await resp_search.json()
        assert "sesión inválida" in body_search["error"]

        # 3. GET /api/stremio/meta without token -> 403 JSON
        resp_meta = await client.get("/api/stremio/meta?id=kitsu:11")
        assert resp_meta.status == 403

        # 4. GET /api/stremio/streams without token -> 403 JSON
        resp_streams = await client.get("/api/stremio/streams?id=kitsu:11")
        assert resp_streams.status == 403

        # 5. GET /api/stremio/voice-channels without token -> 403 JSON
        resp_vc = await client.get("/api/stremio/voice-channels")
        assert resp_vc.status == 403

        # 6. POST /api/stremio/play without token -> 403 JSON
        resp_play = await client.post(
            "/api/stremio/play",
            json={"channel_id": "123456789012345678", "url": "https://example.com/stream.mkv"},
        )
        assert resp_play.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_valid_token_access(mock_bot):
    sess = session_manager.create_session(
        author_id=123,
        author_name="Tester",
        channel_id=456,
        guild_id=789,
    )
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. GET /stremio?token=<token> serves index.html
        resp = await client.get(f"/stremio?token={token}")
        assert resp.status == 200
        text = await resp.text()
        assert "VaPls Stremio" in text

        # 2. GET /stremio/style.css serves static CSS (static assets accessible)
        resp_css = await client.get("/stremio/style.css")
        assert resp_css.status == 200

        # 3. GET /api/stremio/search with token in query
        with patch("torrent_search.search_stremio_catalog", new=AsyncMock(return_value=[{"id": "kitsu:11", "title": "Naruto", "type": "anime"}])):
            resp_search = await client.get(f"/api/stremio/search?q=Naruto&type=anime&token={token}")
            assert resp_search.status == 200
            search_json = await resp_search.json()
            assert len(search_json) == 1
            assert search_json[0]["title"] == "Naruto"

        # 4. GET /api/stremio/meta with X-Stremio-Token header
        with patch("torrent_search.get_stremio_meta", new=AsyncMock(return_value={"id": "kitsu:11", "title": "Naruto", "episodes": []})):
            resp_meta = await client.get("/api/stremio/meta?id=kitsu:11&type=anime", headers={"X-Stremio-Token": token})
            assert resp_meta.status == 200
            meta_json = await resp_meta.json()
            assert meta_json["title"] == "Naruto"

        # 5. GET /api/stremio/streams with token
        with patch("torrent_search.get_stremio_streams", new=AsyncMock(return_value=[{"title": "Naruto Ep 1 1080p", "url": "https://torrentio.strem.fun/resolve/torbox/1/2"}])):
            resp_streams = await client.get(f"/api/stremio/streams?id=kitsu:11&type=anime&season=1&episode=1&token={token}")
            assert resp_streams.status == 200

        # 6. GET /api/stremio/voice-channels with token
        guild_mock = MagicMock()
        guild_mock.id = 789
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

        resp_vc = await client.get(f"/api/stremio/voice-channels?token={token}")
        assert resp_vc.status == 200
        vc_json = await resp_vc.json()
        assert len(vc_json["channels"]) == 1

        # 7. POST /api/stremio/play with token in payload (defaults channel_id and guild_id from session if omitted)
        mock_relay_resp = MagicMock()
        mock_relay_resp.status = 200
        mock_relay_resp.json = AsyncMock(return_value={"status": "ok", "message": "Streaming started"})
        mock_relay_resp.__aenter__.return_value = mock_relay_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_relay_resp):
            resp_play = await client.post(
                "/api/stremio/play",
                json={"token": token, "channel_id": "123456789012345678", "url": "https://torrentio.strem.fun/resolve/torbox/1/2", "title": "Naruto"},
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
    ctx.guild.id = 123
    ctx.author.id = 999
    ctx.author.display_name = "UserTester"
    ctx.author.voice.channel.id = 456
    ctx.channel_id = 789

    # Test /stream stremio command generates tokenized link
    with patch("bot.safe_defer", new=AsyncMock()):
        await stream(ctx, canal="stremio")
        assert ctx.interaction.edit_original_response.called
        kwargs = ctx.interaction.edit_original_response.call_args[1]
        embed = kwargs["embed"]
        view = kwargs["view"]
        assert "Stremio & Anime" in embed.title
        btn = view.children[0]
        assert "token=" in btn.url
        assert "141.148.84.55/stremio?token=" in btn.url or "http" in btn.url


@pytest.mark.asyncio
async def test_stremio_security_validations(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="a", channel_id=100, guild_id=200)
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # Rejects path traversal / invalid characters in item_id
        resp_meta = await client.get(f"/api/stremio/meta?id=../../etc/passwd&type=movie&token={token}")
        assert resp_meta.status == 400

        # Rejects invalid stream URL protocol (e.g. file://)
        resp_play_file = await client.post(
            "/api/stremio/play",
            json={"token": token, "channel_id": "123456789012345678", "url": "file:///etc/passwd", "title": "Test"},
        )
        assert resp_play_file.status == 400

        # Rejects invalid non-numeric channel_id
        resp_play_chan = await client.post(
            "/api/stremio/play",
            json={"token": token, "channel_id": "invalid_chan", "url": "https://example.com/video.mp4", "title": "Test"},
        )
        assert resp_play_chan.status == 400
    finally:
        await client.close()
