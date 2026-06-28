import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Local modules not installed on CI — fake them so golive.bot imports
for _mod in ("video_compat", "davey_compat", "golive_connection", "instagram_feed", "instagram_streamer", "streamer", "ytdlp"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
# discord.voice_state is an internal discord.py sub-module not present in all versions.
# Must also set it as an attr on the real discord module because bot.py does
# ``discord.voice_state.davey = davey_compat`` at import time.
import discord as _discord
_vs = MagicMock()
sys.modules["discord.voice_state"] = _vs
_discord.voice_state = _vs
# golive/bot.py imports the root config.py but expects golive/config.py attrs
import config as _cfg
_cfg.LOG_LEVEL = "INFO"

# ── Helpers ───────────────────────────────────────────────────────────
# yt_dlp is not installed on CI.  We inject a fake module into
# sys.modules so ``import yt_dlp`` inside _yt_extract_url works.

_VID_URL = "https://rr1.example.googlevideo.com/videoplayback?expire=1"
_AUD_URL = "https://rr2.example.googlevideo.com/videoplayback?expire=2"


def _install_fake_ytdlp():
    if "yt_dlp" in sys.modules:
        return sys.modules["yt_dlp"]
    fake = MagicMock()
    sys.modules["yt_dlp"] = fake
    return fake


def _info(**overrides):
    info = {
        "title": "Test Video",
        "live_status": "not_live",
        "formats": [],
        "requested_formats": None,
        "url": None,
    }
    info.update(overrides)
    return info


@pytest.fixture(autouse=True)
def _ensure_fake_ytdlp():
    _install_fake_ytdlp()


# ── _yt_extract_url tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_vod_dash():
    """VOD with requested_formats → returns ( (vid, aud), title, False )."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info(
        requested_formats=[
            {"url": _VID_URL, "protocol": "https"},
            {"url": _AUD_URL, "protocol": "https"},
        ],
        url="https://fallback.example/hls.m3u8",
    )
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url
    result = await _yt_extract_url("https://youtube.com/watch?v=test")

    assert result is not None
    urls, title, is_live = result
    assert isinstance(urls, tuple) and len(urls) == 2
    assert urls[0] == _VID_URL
    assert urls[1] == _AUD_URL
    assert title == "Test Video"
    assert is_live is False


@pytest.mark.asyncio
async def test_extract_live_hls():
    """Live stream → returns best HLS URL."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info(
        live_status="is_live",
        formats=[
            {"protocol": "m3u8_native", "url": "https://hls.example/low.m3u8",
             "vcodec": "avc1", "height": 720, "tbr": 2000},
            {"protocol": "m3u8_native", "url": "https://hls.example/high.m3u8",
             "vcodec": "avc1", "height": 1080, "tbr": 5000},
        ],
    )
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url
    result = await _yt_extract_url("https://youtube.com/watch?v=test")

    url, title, is_live = result
    assert url == "https://hls.example/high.m3u8"
    assert title == "Test Video"
    assert is_live is True


@pytest.mark.asyncio
async def test_extract_single_url():
    """No DASH, no HLS → falls back to info["url"]."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info(url="https://direct.example/video.mp4")
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url
    result = await _yt_extract_url("https://youtube.com/watch?v=test")

    url, title, is_live = result
    assert url == "https://direct.example/video.mp4"
    assert is_live is False


@pytest.mark.asyncio
async def test_extract_no_formats():
    """Nothing useful → returns None."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info()
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url
    result = await _yt_extract_url("https://youtube.com/watch?v=test")
    assert result is None


@pytest.mark.asyncio
async def test_extract_none_info():
    """yt-dlp returns None → returns None."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = None
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url
    result = await _yt_extract_url("https://youtube.com/watch?v=test")
    assert result is None


@pytest.mark.asyncio
async def test_extract_title_fallback():
    """Missing title → "YouTube Stream"."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info(title=None, url="https://direct.example/v.mp4")
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url
    _, title, _ = await _yt_extract_url("https://youtube.com/watch?v=test")
    assert title == "YouTube Stream"


@pytest.mark.asyncio
async def test_extract_cookies_and_pot(tmp_path):
    """cookie file and POT args are forwarded to YoutubeDL opts."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("")

    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info(
        requested_formats=[
            {"url": _VID_URL, "protocol": "https"},
            {"url": _AUD_URL, "protocol": "https"},
        ],
    )
    fake_ytdlp.YoutubeDL.return_value = ydl

    with (
        patch("golive.ytdlp._get_cookies_path", return_value=str(cookies)),
        patch("golive.ytdlp._get_extractor_args",
              return_value={"youtubepot-bgutilhttp": {"base_url": "http://127.0.0.1:4416"}}),
    ):
        from golive.ytdlp import _yt_extract_url
        await _yt_extract_url("https://youtube.com/watch?v=test")

        args, _ = fake_ytdlp.YoutubeDL.call_args
        opts = args[0] if args else {}
        assert opts["cookiefile"] == str(cookies)
        assert opts["extractor_args"] == {
            "youtubepot-bgutilhttp": {"base_url": "http://127.0.0.1:4416"},
        }


@pytest.mark.asyncio
async def test_extract_no_cookies_no_pot():
    """No cookie file, no POT → opts lack those keys."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _info(
        requested_formats=[
            {"url": _VID_URL, "protocol": "https"},
            {"url": _AUD_URL, "protocol": "https"},
        ],
    )
    fake_ytdlp.YoutubeDL.return_value = ydl

    with (
        patch("golive.ytdlp._get_cookies_path", return_value=None),
        patch("golive.ytdlp._get_extractor_args", return_value=None),
    ):
        from golive.ytdlp import _yt_extract_url
        await _yt_extract_url("https://youtube.com/watch?v=test")

        args, _ = fake_ytdlp.YoutubeDL.call_args
        opts = args[0] if args else {}
        assert "cookiefile" not in opts
        assert "extractor_args" not in opts


# ── GoLiveStream.start() tests ────────────────────────────────────────


@pytest.fixture
def mock_golive_infra(monkeypatch):
    """Isolate GoLiveStream.start() from network / threads."""
    conn = AsyncMock()
    conn.ssrc = 1000
    monkeypatch.setattr("golive.bot.GoLiveConnection", lambda *a, **k: conn)
    monkeypatch.setattr("asyncio.to_thread", lambda fn, *a: MagicMock())
    monkeypatch.setattr("golive.bot.GoLiveStream._start_players", AsyncMock())
    return conn


def _make_stream(url="https://youtube.com/watch?v=test"):
    from golive.bot import GoLiveStream
    return GoLiveStream(
        bot=MagicMock(), guild_id=1, channel_id=2,
        vc=MagicMock(), url=url,
    )


@pytest.mark.asyncio
async def test_start_dash_tuple(mock_golive_infra):
    """DASH tuple sets target_url to (vid, aud) and is_live False."""
    stream = _make_stream()
    extracted = ((_VID_URL, _AUD_URL), "Test Vid", False)
    with patch("ytdlp._yt_extract_url", AsyncMock(return_value=extracted)):
        await stream.start()
    assert stream.target_url == (_VID_URL, _AUD_URL)
    assert stream.title == "Test Vid"
    assert stream.is_live is False


@pytest.mark.asyncio
async def test_start_single_url(mock_golive_infra):
    """Single URL sets target_url to str and is_live True."""
    stream = _make_stream()
    extracted = ("https://direct.example/stream.m3u8", "Live Stream", True)
    with patch("ytdlp._yt_extract_url", AsyncMock(return_value=extracted)):
        await stream.start()
    assert stream.target_url == "https://direct.example/stream.m3u8"
    assert stream.title == "Live Stream"
    assert stream.is_live is True


@pytest.mark.asyncio
async def test_start_extraction_none(mock_golive_infra):
    """Extraction returns None → RuntimeError."""
    stream = _make_stream()
    with patch("ytdlp._yt_extract_url", AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="Failed to extract stream URL"):
            await stream.start()


@pytest.mark.asyncio
async def test_start_extraction_exception(mock_golive_infra):
    """Extraction raises → RuntimeError."""
    stream = _make_stream()
    with patch("ytdlp._yt_extract_url",
               AsyncMock(side_effect=Exception("network error"))):
        with pytest.raises(RuntimeError, match="Failed to extract stream URL"):
            await stream.start()


@pytest.mark.asyncio
async def test_start_direct_hls_skips_ytdlp(mock_golive_infra):
    """.m3u8 URL → skips yt-dlp entirely."""
    stream = _make_stream(url="https://cdn.example/stream.m3u8")
    spy = MagicMock()
    with patch("ytdlp._yt_extract_url", spy):
        await stream.start()
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_start_direct_mpd_skips_ytdlp(mock_golive_infra):
    """.mpd URL → skips yt-dlp entirely."""
    stream = _make_stream(url="https://cdn.example/stream.mpd")
    spy = MagicMock()
    with patch("ytdlp._yt_extract_url", spy):
        await stream.start()
    spy.assert_not_called()


# ── Format string test ────────────────────────────────────────────────

def test_format_string_excludes_av1():
    import inspect
    from golive.ytdlp import _yt_extract_url
    src = inspect.getsource(_yt_extract_url)
    assert "vcodec!*=av01" in src, "Format string must exclude AV1"
    assert "bestvideo" in src, "Format string must use bestvideo"
