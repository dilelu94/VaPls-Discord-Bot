"""Behavioral tests for Stremio & GoLive audio and subtitle track configuration endpoints and VideoPlayer logic."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiohttp.test_utils import TestClient, TestServer

import apiServer
from media_inspector import AudioTrack, MediaTracksInfo, SubtitleTrack
from stremio_sessions import session_manager


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


@pytest.mark.asyncio
async def test_stremio_tracks_unauthenticated_rejected(mock_bot):
    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        resp = await client.get("/api/stremio/tracks?url=https://example.com/movie.mp4")
        assert resp.status == 403
        data = await resp.json()
        assert "sesión inválida" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_tracks_authenticated_success(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    mock_info = MediaTracksInfo(
        url="https://example.com/movie.mp4",
        audio_tracks=[
            AudioTrack(index=0, stream_index=1, language="ja", title="Original", codec="aac", channels=2),
            AudioTrack(index=1, stream_index=2, language="es", title="Latino", codec="aac", channels=2),
        ],
        subtitle_tracks=[
            SubtitleTrack(index=0, stream_index=3, language="es", title="Completo", codec="subrip", is_forced=False),
        ],
    )

    try:
        with patch("media_inspector.inspect_media_tracks", new=AsyncMock(return_value=mock_info)):
            resp = await client.get(f"/api/stremio/tracks?url=https://example.com/movie.mp4&token={token}")
            assert resp.status == 200
            data = await resp.json()
            assert data["has_multiple_audios"] is True
            assert data["has_subtitles"] is True
            assert len(data["audio_tracks"]) == 2
            assert len(data["subtitle_tracks"]) == 1
            assert data["audio_tracks"][0]["language"] == "ja"
            assert data["subtitle_tracks"][0]["language"] == "es"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_play_with_custom_tracks(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        mock_relay_resp = MagicMock()
        mock_relay_resp.status = 200
        mock_relay_resp.json = AsyncMock(return_value={"started": True, "guild_id": 200})
        mock_relay_resp.__aenter__.return_value = mock_relay_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_relay_resp):
            resp = await client.post(
                "/api/stremio/play",
                json={
                    "token": token,
                    "channel_id": "100",
                    "guild_id": "200",
                    "url": "https://example.com/movie.mp4",
                    "title": "Custom Tracks Movie",
                    "audio_track": 1,
                    "subtitle_track": 0,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["started"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stremio_control_set_tracks_action(mock_bot):
    sess = session_manager.create_session(author_id=1, author_name="Tester", channel_id=100, guild_id=200)
    token = sess.token

    app = apiServer.makeApp(mock_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        mock_control_resp = MagicMock()
        mock_control_resp.status = 200
        mock_control_resp.json = AsyncMock(return_value={"status": "tracks_updated", "audio_track": 1, "subtitle_track": 0})
        mock_control_resp.__aenter__.return_value = mock_control_resp

        with patch("aiohttp.ClientSession.post", return_value=mock_control_resp):
            resp = await client.post(
                "/api/stremio/control",
                json={
                    "token": token,
                    "action": "set_tracks",
                    "guild_id": "200",
                    "audio_track": 1,
                    "subtitle_track": 0,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "tracks_updated"
            assert data["audio_track"] == 1
    finally:
        await client.close()


def test_h264_video_player_set_tracks_triggers_seek():
    from golive.slopsoil.video_player import H264VideoPlayer
    vc_mock = MagicMock()
    vc_mock.ssrc = 1000

    player = H264VideoPlayer(
        url="https://example.com/stream.mp4",
        voice_client=vc_mock,
        audio_track=0,
        subtitle_track=-1,
    )

    with patch.object(player, "seek") as mock_seek:
        player.set_tracks(audio_track=2, subtitle_track=1)
        assert player._audio_track == 2
        assert player._subtitle_track == 1
        mock_seek.assert_called_once()
