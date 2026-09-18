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

        with patch("aiohttp.ClientSession.post", return_value=mock_relay_resp), \
             patch("torrent_search.resolve_redirect_url", return_value="https://nexus-001.tb-cdn.io/stream.mkv"):
            resp_play = await client.post(
                "/api/stremio/play",
                json={"token": token, "channel_id": "123456789012345678", "url": "https://torrentio.strem.fun/resolve/torbox/1/2", "title": "Naruto"},
            )
            assert resp_play.status == 200
            play_json = await resp_play.json()
            assert play_json["status"] == "ok"
            from bot import _active_sources
            assert _active_sources.get(789) is not None
            assert _active_sources[789]["type"] == "stremio"
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


def test_watched_manager_lifecycle(tmp_path):
    import stremio_sessions
    storage_file = str(tmp_path / "stremio_watched.json")
    wm = stremio_sessions.WatchedManager(storage_path=storage_file)
    assert wm.get_all() == {}

    res = wm.set_watched("tt0944947:s1:e1", True)
    assert "tt0944947:s1:e1" in res

    wm2 = stremio_sessions.WatchedManager(storage_path=storage_file)
    assert "tt0944947:s1:e1" in wm2.get_all()

    res_unwatched = wm.set_watched("tt0944947:s1:e1", False)
    assert "tt0944947:s1:e1" not in res_unwatched


@pytest.mark.asyncio
async def test_stremio_watched_api_endpoints(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="a", channel_id=100, guild_id=200)
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # GET /api/stremio/watched with valid token
        resp_get = await client.get(f"/api/stremio/watched?token={token}")
        assert resp_get.status == 200
        data_get = await resp_get.json()
        assert "watched" in data_get

        # POST /api/stremio/watched mark episode as watched
        resp_post = await client.post(
            "/api/stremio/watched",
            json={"token": token, "key": "kitsu:11:s1:e2", "watched": True},
        )
        assert resp_post.status == 200
        data_post = await resp_post.json()
        assert data_post["ok"] is True
        assert "kitsu:11:s1:e2" in data_post["watched"]

        # POST /api/stremio/watched mark episode as unwatched
        resp_unwatch = await client.post(
            "/api/stremio/watched",
            json={"token": token, "key": "kitsu:11:s1:e2", "watched": False},
        )
        assert resp_unwatch.status == 200
        data_unwatch = await resp_unwatch.json()
        assert "kitsu:11:s1:e2" not in data_unwatch["watched"]
    finally:
        await client.close()


def test_stremio_session_extension():
    sess = session_manager.create_session(
        author_id=100,
        author_name="Extender",
        channel_id=200,
        guild_id=300,
        ttl_hours=10 / 60.0,
    )
    initial_expiry = sess.expires_at

    # Extend session for a 2-hour movie (7200 seconds)
    extended = session_manager.extend_session(sess.token, 7200.0)
    assert extended is not None
    assert extended.expires_at > initial_expiry
    # TTL should be ~7800 seconds (7200s + 600s buffer)
    assert extended.expires_at - time.time() >= 7700.0


@pytest.mark.asyncio
async def test_stremio_control_api_endpoints(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="a", channel_id=100, guild_id=200)
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. Reject without token -> 403
        resp_no_token = await client.get("/api/stremio/control?action=status")
        assert resp_no_token.status == 403

        # 2. GET /api/stremio/control?action=status with token
        mock_control_resp = MagicMock()
        mock_control_resp.status = 200
        mock_control_resp.json = AsyncMock(return_value={"exists": True, "position": 12.5, "is_paused": False, "title": "Test Movie"})
        mock_control_resp.__aenter__.return_value = mock_control_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_control_resp):
            resp_status = await client.get(f"/api/stremio/control?action=status&token={token}")
            assert resp_status.status == 200
            data_status = await resp_status.json()
            assert data_status["position"] == 12.5
            assert data_status["title"] == "Test Movie"

        # 3. POST /api/stremio/control action=pause
        mock_pause_resp = MagicMock()
        mock_pause_resp.status = 200
        mock_pause_resp.json = AsyncMock(return_value={"status": "paused", "guild_id": 200})
        mock_pause_resp.__aenter__.return_value = mock_pause_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_pause_resp):
            resp_pause = await client.post(
                "/api/stremio/control",
                json={"token": token, "action": "pause", "guild_id": "200"},
            )
            assert resp_pause.status == 200
            data_pause = await resp_pause.json()
            assert data_pause["status"] == "paused"

        # 4. POST /api/stremio/control action=stop
        with patch("bot.stop_stream_for_guild", new=AsyncMock(return_value=(True, "🛑 Stream detenido."))) as mock_stop:
            resp_stop = await client.post(
                "/api/stremio/control",
                json={"token": token, "action": "stop", "guild_id": "200"},
            )
            assert resp_stop.status == 200
            data_stop = await resp_stop.json()
            assert data_stop["stopped"] is True
            mock_stop.assert_called_once_with(200)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_play_extends_session(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="a", channel_id=100, guild_id=200, ttl_hours=10/60.0)
    token = sess.token
    initial_expires_at = sess.expires_at

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        mock_relay_resp = MagicMock()
        mock_relay_resp.status = 200
        mock_relay_resp.json = AsyncMock(return_value={"started": True, "guild_id": 200})
        mock_relay_resp.__aenter__.return_value = mock_relay_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_relay_resp), \
             patch("torrent_search.resolve_redirect_url", return_value="https://nexus-001.tb-cdn.io/stream.mkv"):
            resp_play = await client.post(
                "/api/stremio/play",
                json={
                    "token": token,
                    "channel_id": "123456789012345678",
                    "guild_id": "200",
                    "url": "https://torrentio.strem.fun/resolve/torbox/1/2",
                    "title": "Movie 2 Hours",
                    "duration": 7200.0, # 2 hours in seconds
                },
            )
            assert resp_play.status == 200
            play_json = await resp_play.json()
            assert play_json["started"] is True
            assert "expires_at" in play_json
            assert play_json["expires_at"] > initial_expires_at
            # Updated session object in manager has new expiry
            updated_sess = session_manager.get_session(token)
            assert updated_sess.expires_at > initial_expires_at
    finally:
        await client.close()

