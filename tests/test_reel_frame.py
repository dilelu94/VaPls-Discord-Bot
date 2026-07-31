import asyncio
import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web

import discord

if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

import config
from apiServer import makeApp

import reelFrame


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.guilds = []
    return bot


@pytest.fixture
async def local_server(mock_bot):
    config.API_SECRET = "test_secret_key_123"
    config.API_HOST = "127.0.0.1"
    config.API_PORT = 9998
    app = makeApp(mock_bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 9998)
    await site.start()
    yield "http://127.0.0.1:9998"
    await runner.cleanup()


def _jpeg():
    return b"\xff\xd8\xff\xe0" + b"\x00" * 64


async def _post(local_server, payload):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_server}/instagram/generate-reply",
            json=payload,
            headers={"X-API-Secret": "test_secret_key_123"},
        ) as resp:
            return resp.status, await resp.json()


def test_frame_offset_clamping():
    assert reelFrame._frame_offset(None) == 3.0
    assert reelFrame._frame_offset(0) == 3.0
    assert reelFrame._frame_offset(1.5) == 3.0
    assert reelFrame._frame_offset(10) == 3.0
    assert reelFrame._frame_offset(2) == 1.0
    assert reelFrame._frame_offset(100) == 30.0


async def test_grab_frame_success():
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(_jpeg(), b""))
    with patch.object(reelFrame, "shutil") as sh, \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mk:
        sh.which.return_value = "/usr/bin/ffmpeg"
        out = await reelFrame.grab_frame("http://cdn/v.mp4", 20.0)
        assert out == _jpeg()
        args = mk.call_args.args
        assert args[0] == "/usr/bin/ffmpeg"
        assert "-ss" in args
        assert "6.0" in args


async def test_grab_frame_failure_returns_none():
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"some error"))
    with patch.object(reelFrame, "shutil") as sh, \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        sh.which.return_value = "/usr/bin/ffmpeg"
        assert await reelFrame.grab_frame("http://cdn/v.mp4") is None


async def test_grab_frame_timeout_returns_none():
    proc = MagicMock()
    proc.kill = MagicMock()
    proc.communicate = AsyncMock()
    with patch.object(reelFrame, "shutil") as sh, \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        sh.which.return_value = "/usr/bin/ffmpeg"
        assert await reelFrame.grab_frame("http://cdn/v.mp4") is None
        proc.kill.assert_called_once()


async def test_grab_frame_ffmpeg_missing_returns_none():
    with patch.object(reelFrame, "shutil") as sh, \
         patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no ffmpeg")):
        sh.which.return_value = None
        assert await reelFrame.grab_frame("http://cdn/v.mp4") is None


@patch("reelFrame.grab_frame", new=AsyncMock(return_value=_jpeg()))
@patch("geminiCommand.indioInstagramScraperLogic")
@patch("geminiCommand.describe_image")
async def test_api_generate_reply_video_url_uses_frame(mock_describe, mock_logic, local_server):
    mock_describe.return_value = "un partido de fútbol"
    mock_logic.return_value = {"reply": "jaja", "react": None}
    status, _ = await _post(local_server, {
        "username": "mati",
        "text": "mira esto",
        "reel_caption": "golazo",
        "video_url": "http://cdn/v.mp4",
        "video_duration": 20.0,
        "is_reel_mention": True,
    })
    assert status == 200
    mock_describe.assert_awaited_once()
    assert mock_describe.await_args.args[0] == _jpeg()
    _, kwargs = mock_logic.call_args
    assert kwargs["is_reel_mention"] is True


@patch("reelFrame.grab_frame", new=AsyncMock(return_value=None))
@patch("geminiCommand.indioInstagramScraperLogic")
@patch("geminiCommand.describe_image")
async def test_api_generate_reply_video_frame_fallback_thumbnail(mock_describe, mock_logic, local_server):
    thumb = _jpeg() + b"\x01"
    thumb_b64 = base64.b64encode(thumb).decode()
    mock_describe.return_value = "una foto"
    mock_logic.return_value = {"reply": "ok", "react": None}
    status, _ = await _post(local_server, {
        "username": "mati",
        "text": "hola",
        "video_url": "http://cdn/v.mp4",
        "image_b64": thumb_b64,
        "is_reel_mention": True,
    })
    assert status == 200
    mock_describe.assert_awaited_once()
    assert mock_describe.await_args.args[0] == thumb


async def test_fetch_gemini_reply_payload(monkeypatch):
    scraper_dir = Path(__file__).resolve().parent.parent / "instagram_scraper"
    sys.path.insert(0, str(scraper_dir))
    import scraper

    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["payload"] = json
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"reply": "hola", "react": None}
        return resp

    monkeypatch.setattr(scraper.requests, "post", fake_post)
    out = scraper.fetch_gemini_reply("mati", "hola", "cap", "b64", "http://cdn/v.mp4", 12.5, True)
    assert out["reply"] == "hola"
    assert captured["url"] == "http://localhost:8080/instagram/generate-reply"
    payload = captured["payload"]
    assert payload["username"] == "mati"
    assert payload["text"] == "hola"
    assert payload["reel_caption"] == "cap"
    assert payload["image_b64"] == "b64"
    assert payload["video_url"] == "http://cdn/v.mp4"
    assert payload["video_duration"] == 12.5
    assert payload["is_reel_mention"] is True
