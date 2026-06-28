import json
import sys
import urllib.error
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Local modules not installed on CI — fake them so golive.bot imports
for _mod in ("video_compat", "davey_compat", "golive_connection", "instagram_feed", "instagram_streamer", "streamer", "ytdlp"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
# discord.voice is a py-cord package that checks for PyNaCl at import time.
# Mock the whole package so golive/bot.py's ``import discord.voice_state``
# never triggers the real check (fails on Python 3.10 CI, no-op on 3.14+).
import discord as _discord
_vs = MagicMock()
_vp = MagicMock()
sys.modules["discord.voice_state"] = _vs
sys.modules["discord.voice"] = _vp
_discord.voice_state = _vs
_discord.voice = _vp
# Prevent module-level discord.Client(chunk_guilds_at_startup=False) from needing
# an event loop at import time (fails on Python 3.10, handled gracefully on 3.14+).
_discord.Client = MagicMock()
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


# ── _instagram_api_reel_feed_urls tests ───────────────────────────────

_MOCK_COOKIE = type("MockCookie", (), {"name": "sessionid", "value": "abc123", "domain": ".instagram.com", "path": "/"})()


def _make_fake_cj():
    """Return a MozillaCookieJar-like mock that yields one sessionid cookie."""
    cj = MagicMock()
    cj.__iter__.return_value = iter([_MOCK_COOKIE])
    return cj


def _make_fake_opener(body: bytes):
    """Return a build_opener()-return mock whose open() yields ``body``."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = body
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value = resp
    return opener


class TestInstagramApiReelFeedUrls:
    """_instagram_api_reel_feed_urls — direct Instagram Web API timeline feed."""

    def test_success_returns_reels_filters_photos(self):
        """Reels (media_type=2) are returned; photos (media_type=1) are skipped."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        body = json.dumps({
            "items": [
                {"media_type": 2, "code": "abc123"},
                {"media_type": 1, "code": "photo1"},
                {"media_type": 2, "code": "reel456"},
            ],
            "next_max_id": None,
        }).encode()

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=_make_fake_opener(body)),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == [
            "https://www.instagram.com/reel/abc123/",
            "https://www.instagram.com/reel/reel456/",
        ]

    def test_handles_media_or_ad_wrapper(self):
        """Some feed items wrap media in 'media_or_ad' — unpack it."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        body = json.dumps({
            "items": [
                {"media_or_ad": {"media_type": 2, "code": "wrapped1"}},
                {"media_or_ad": {"media_type": 1, "code": "adphoto"}},
                {"media_type": 2, "code": "plain1"},
            ],
            "next_max_id": None,
        }).encode()

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=_make_fake_opener(body)),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == [
            "https://www.instagram.com/reel/wrapped1/",
            "https://www.instagram.com/reel/plain1/",
        ]

    def test_paginates_via_next_max_id(self):
        """When next_max_id is set, continues fetching until limit reached."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        page1 = json.dumps({
            "items": [{"media_type": 2, "code": f"page1_{i}"} for i in range(3)],
            "next_max_id": "abc123",
        }).encode()
        page2 = json.dumps({
            "items": [{"media_type": 2, "code": f"page2_{i}"} for i in range(3)],
            "next_max_id": None,
        }).encode()

        resp = MagicMock()
        resp.status = 200
        resp.read.side_effect = [page1, page2]
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = resp

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=opener),
        ):
            result = _instagram_api_reel_feed_urls(limit=5)

        assert len(result) == 5
        assert result[0] == "https://www.instagram.com/reel/page1_0/"
        assert result[4] == "https://www.instagram.com/reel/page2_1/"

    def test_empty_items_returns_empty(self):
        """API returns no items → empty list."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        body = json.dumps({"items": [], "next_max_id": None}).encode()

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=_make_fake_opener(body)),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == []

    def test_no_cookies_file_returns_empty(self):
        """Missing instagram_cookies.txt → empty list."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        with patch("golive.ytdlp._get_instagram_cookies_path", return_value=None):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == []

    def test_no_sessionid_returns_empty(self):
        """Cookie jar without sessionid → empty list."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        cj = MagicMock()
        bad = MagicMock()
        bad.name = "csrftoken"
        bad.value = "xyz"
        cj.__iter__.return_value = iter([bad])

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=cj),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == []

    def test_http_error_returns_empty(self):
        """HTTP error during request → empty list."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("mock error")

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=opener),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == []

    def test_malformed_json_returns_empty(self):
        """Non-JSON response → empty list."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=_make_fake_opener(b"not json")),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == []

    def test_empty_video_code_skipped(self):
        """Item with media_type=2 but empty code → skipped."""
        from golive.ytdlp import _instagram_api_reel_feed_urls

        body = json.dumps({
            "items": [
                {"media_type": 2, "code": ""},
                {"media_type": 2, "code": "valid1"},
            ],
            "next_max_id": None,
        }).encode()

        with (
            patch("golive.ytdlp._get_instagram_cookies_path", return_value="/f/cookies.txt"),
            patch("http.cookiejar.MozillaCookieJar", return_value=_make_fake_cj()),
            patch("urllib.request.build_opener", return_value=_make_fake_opener(body)),
        ):
            result = _instagram_api_reel_feed_urls(limit=10)

        assert result == ["https://www.instagram.com/reel/valid1/"]
