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
        assert "válido por la duración del stream" in text

        # 1b. GET / without token serves the public landing page (200 OK)
        resp_root = await client.get("/")
        assert resp_root.status == 200
        text_root = await resp_root.text()
        assert "VaPls &amp; El Indio" in text_root or "VaPls & El Indio" in text_root

        # 1c. GET / with invalid token delegates to Stremio -> 403 Access Denied
        resp_root_tok = await client.get("/?token=invalid_stremio_token_123456789")
        assert resp_root_tok.status == 403
        text_root_tok = await resp_root_tok.text()
        assert "Acceso Denegado" in text_root_tok
        assert "/stream stremio" in text_root_tok

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
        assert 'id="navAllow4kCheckbox"' in text
        assert 'id="modalAllow4kCheckbox"' in text
        assert 'checked' not in text.split('id="navAllow4kCheckbox"')[1].split('>')[0]
        assert 'checked' not in text.split('id="modalAllow4kCheckbox"')[1].split('>')[0]

        # 1b. GET /?token=<token> serves index.html
        resp_root = await client.get(f"/?token={token}")
        assert resp_root.status == 200
        text_root = await resp_root.text()
        assert "VaPls Stremio" in text_root

        # 1c. GET /<token> serves index.html
        resp_path = await client.get(f"/{token}")
        assert resp_path.status == 200
        text_path = await resp_path.text()
        assert "VaPls Stremio" in text_path

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

        # 5. GET /api/stremio/streams with token (test filtering with allow_4k=0)
        mock_streams = [
            {"title": "Naruto Ep 1 4K UHD", "quality": "4K", "url": "https://torrentio.strem.fun/resolve/torbox/1/4k"},
            {"title": "Naruto Ep 1 2K QHD", "quality": "2K", "url": "https://torrentio.strem.fun/resolve/torbox/1/2k"},
            {"title": "Naruto Ep 1 1080p", "quality": "1080p", "url": "https://torrentio.strem.fun/resolve/torbox/1/1080p"},
        ]
        with patch("torrent_search.get_stremio_streams", new=AsyncMock(return_value=mock_streams)):
            # All streams returned by default
            resp_all = await client.get(f"/api/stremio/streams?id=kitsu:11&type=anime&season=1&episode=1&token={token}")
            assert resp_all.status == 200
            all_json = await resp_all.json()
            assert len(all_json) == 3

            # Filtered streams when allow_4k=0
            resp_filtered = await client.get(f"/api/stremio/streams?id=kitsu:11&type=anime&season=1&episode=1&allow_4k=0&token={token}")
            assert resp_filtered.status == 200
            filtered_json = await resp_filtered.json()
            assert len(filtered_json) == 1
            assert filtered_json[0]["quality"] == "1080p"

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

    # Test /stream stremio command generates tokenized link with https://vapls.duckdns.org/
    with patch("bot.safe_defer", new=AsyncMock()):
        await stream(ctx, opcion="stremio")
        assert ctx.interaction.edit_original_response.called
        kwargs = ctx.interaction.edit_original_response.call_args[1]
        embed = kwargs["embed"]
        view = kwargs["view"]
        assert "Stremio & Anime" in embed.title
        btn = view.children[0]
        assert "token=" in btn.url
        assert "vapls.duckdns.org/?token=" in btn.url or "https://vapls.duckdns.org" in btn.url


@pytest.mark.asyncio
async def test_stremio_slash_command_custom_url(monkeypatch):
    from bot import stream
    import config

    monkeypatch.setattr(config, "STREMIO_WEB_URL", "https://custom.duckdns.org/stremio")

    ctx = AsyncMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 999
    ctx.author.display_name = "UserTester"
    ctx.author.voice.channel.id = 456
    ctx.channel_id = 789

    with patch("bot.safe_defer", new=AsyncMock()):
        await stream(ctx, opcion="stremio")
        assert ctx.interaction.edit_original_response.called
        kwargs = ctx.interaction.edit_original_response.call_args[1]
        btn = kwargs["view"].children[0]
        assert "https://custom.duckdns.org/stremio?token=" in btn.url


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
    sess = session_manager.create_session(author_id=1, author_name="a", channel_id=123456789012345678, guild_id=200, ttl_hours=10/60.0)
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


@pytest.mark.asyncio
async def test_stremio_session_touch_on_validation(mock_bot):
    """Test that calling API endpoints automatically touches/refreshes the session token so it never expires during active use."""
    import bot
    bot._active_sources[200] = {"type": "stremio"}
    try:
        sess = session_manager.create_session(author_id=1, author_name="a", channel_id=100, guild_id=200, ttl_hours=0.5)
        token = sess.token
        # Set expiration to 60 seconds from now
        sess.expires_at = time.time() + 60.0

        app = apiServer.makeApp(mock_bot)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            mock_control_resp = MagicMock()
            mock_control_resp.status = 200
            mock_control_resp.json = AsyncMock(return_value={"exists": True, "position": 5.0})
            mock_control_resp.__aenter__.return_value = mock_control_resp

            with patch("aiohttp.ClientSession.post", return_value=mock_control_resp):
                resp = await client.get(f"/api/stremio/control?action=status&token={token}")
                assert resp.status == 200

            # After status call while active, session should be touched and extended (at least 300s TTL)
            updated_sess = session_manager.get_session(token)
            assert updated_sess is not None
            assert updated_sess.expires_at - time.time() >= 250.0
        finally:
            await client.close()
    finally:
        bot._active_sources.pop(200, None)


@pytest.mark.asyncio
async def test_revoke_sessions_for_guild():
    sess1 = session_manager.create_session(author_id=1, author_name="user1", channel_id=100, guild_id=999)
    sess2 = session_manager.create_session(author_id=2, author_name="user2", channel_id=100, guild_id=999)
    sess3 = session_manager.create_session(author_id=3, author_name="user3", channel_id=100, guild_id=888)

    assert session_manager.validate_token(sess1.token) is True
    assert session_manager.validate_token(sess2.token) is True
    assert session_manager.validate_token(sess3.token) is True

    revoked_count = session_manager.revoke_sessions_for_guild(999)
    assert revoked_count == 2
    assert session_manager.validate_token(sess1.token) is False
    assert session_manager.validate_token(sess2.token) is False
    assert session_manager.validate_token(sess3.token) is True


@pytest.mark.asyncio
async def test_revoke_sessions_for_guild_zero_does_not_wipe_other_guilds():
    sess_dm = session_manager.create_session(author_id=1, author_name="dm_user", channel_id=100, guild_id=0)
    sess_guild = session_manager.create_session(author_id=2, author_name="guild_user", channel_id=200, guild_id=999)

    assert session_manager.validate_token(sess_dm.token) is True
    assert session_manager.validate_token(sess_guild.token) is True

    revoked_count = session_manager.revoke_sessions_for_guild(0)
    assert revoked_count == 1
    assert session_manager.validate_token(sess_dm.token) is False
    assert session_manager.validate_token(sess_guild.token) is True


@pytest.mark.asyncio
async def test_bot_voice_disconnect_does_not_revoke_stremio_session():
    from bot import on_voice_state_update, bot

    sess = session_manager.create_session(author_id=1, author_name="user", channel_id=100, guild_id=555)
    assert session_manager.validate_token(sess.token) is True

    member = bot.user
    before = MagicMock()
    before.channel.guild.id = 555
    before.channel.name = "General Voice"
    after = MagicMock()
    after.channel = None  # Bot leaves voice channel

    with patch("bot.analytics.capture"):
        await on_voice_state_update(member, before, after)

    # Session token MUST still be valid after main bot voice disconnect
    assert session_manager.validate_token(sess.token) is True


@pytest.mark.asyncio
async def test_stop_stream_for_guild_revokes_sessions():
    from bot import stop_stream_for_guild

    sess = session_manager.create_session(author_id=1, author_name="user1", channel_id=100, guild_id=777)
    assert session_manager.validate_token(sess.token) is True

    with patch("config.GOLIVE_RELAY_URL", "http://127.0.0.1:8082"), \
         patch("config.GOLIVE_RELAY_SECRET", "secret"):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value="OK")
        mock_resp.__aenter__.return_value = mock_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_resp):
            success, msg = await stop_stream_for_guild(777)
            assert success is True

    assert session_manager.validate_token(sess.token) is False


@pytest.mark.asyncio
async def test_stremio_security_headers_present(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        resp = await client.get(f"/stremio?token={sess.token}")
        assert resp.status == 200
        headers = resp.headers
        assert "Content-Security-Policy" in headers
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("Referrer-Policy") == "no-referrer"

        # Headers also present on root /?token=
        resp_root = await client.get(f"/?token={sess.token}")
        assert resp_root.status == 200
        headers_root = resp_root.headers
        assert "Content-Security-Policy" in headers_root
        assert headers_root.get("X-Frame-Options") == "DENY"
        assert headers_root.get("X-Content-Type-Options") == "nosniff"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_ssrf_prevention(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        forbidden_urls = [
            "http://127.0.0.1:8080/admin",
            "http://localhost:8082/stream",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/secret",
            "http://192.168.1.10/router",
            "http://0.0.0.0/",
            "http://[::1]/",
            "http://myhost.local/",
            "http://internal.service.internal/",
        ]
        for url in forbidden_urls:
            resp = await client.post(
                "/api/stremio/play",
                json={"token": sess.token, "channel_id": "123456789012345678", "url": url, "title": "SSRF Test"},
            )
            assert resp.status == 400, f"Expected 400 for forbidden URL: {url}"
            data = await resp.json()
            assert "invalid or unsafe stream url" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_invalid_token_format_rejected(mock_bot):
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        malicious_tokens = [
            "../../../etc/passwd",
            "<script>alert(1)</script>",
            "' OR 1=1--",
            "not_hex_token_32chars_long_xxxx",
            "12345",
        ]
        for tok in malicious_tokens:
            resp = await client.get(f"/stremio?token={tok}")
            assert resp.status == 403
            resp_api = await client.get(f"/api/stremio/search?q=test&token={tok}")
            assert resp_api.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_control_invalid_actions_rejected(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        invalid_actions = ["exec", "eval", "system", "delete", "format_c"]
        for act in invalid_actions:
            resp = await client.post(
                "/api/stremio/control",
                json={"token": sess.token, "action": act, "guild_id": "200"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "invalid action"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_watched_invalid_key_rejected(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        invalid_keys = [
            "../../etc/passwd",
            "<script>alert(1)</script>",
            "key with spaces and ' quotes",
            "key;drop table users;",
        ]
        for key in invalid_keys:
            resp = await client.post(
                "/api/stremio/watched",
                json={"token": sess.token, "key": key, "watched": True},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "invalid key" in data["error"]
    finally:
        await client.close()




