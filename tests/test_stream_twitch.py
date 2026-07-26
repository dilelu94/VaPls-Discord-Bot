import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Fake local golive / py-cord modules if not present (mirroring test_golive_ytdlp.py setup)
for _mod in ("video_compat", "davey_compat", "golive_connection", "instagram_feed", "instagram_streamer", "streamer", "ytdlp"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import discord as _discord
if not hasattr(_discord, "voice_state"):
    _vs = MagicMock()
    _vp = MagicMock()
    sys.modules["discord.voice_state"] = _vs
    sys.modules["discord.voice"] = _vp
    _discord.voice_state = _vs
    _discord.voice = _vp

def _install_fake_ytdlp():
    if "yt_dlp" in sys.modules:
        return sys.modules["yt_dlp"]
    fake = MagicMock()
    sys.modules["yt_dlp"] = fake
    return fake


@pytest.fixture(autouse=True)
def _ensure_fake_ytdlp():
    _install_fake_ytdlp()


def _twitch_info(**overrides):
    info = {
        "title": "ibai - DIRECTO DE IBAI",
        "uploader": "ibai",
        "is_live": True,
        "live_status": "is_live",
        "formats": [
            {
                "protocol": "m3u8_native",
                "url": "https://video-weaver.sjc01.hls.ttvnw.net/v1/playlist/high.m3u8",
                "vcodec": "avc1.4D401F",
                "height": 1080,
                "tbr": 6000,
            },
            {
                "protocol": "m3u8_native",
                "url": "https://video-weaver.sjc01.hls.ttvnw.net/v1/playlist/low.m3u8",
                "vcodec": "avc1.4D401F",
                "height": 720,
                "tbr": 3000,
            },
        ],
        "requested_formats": None,
        "url": None,
    }
    info.update(overrides)
    return info


@pytest.mark.asyncio
async def test_extract_twitch_live_stream():
    """Twitch live stream URL returns the best quality m3u8 HLS playlist and title."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _twitch_info()
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url

    url, title, is_live = await _yt_extract_url("https://www.twitch.tv/ibai?lang=es")

    assert url == "https://video-weaver.sjc01.hls.ttvnw.net/v1/playlist/high.m3u8"
    assert title == "ibai - DIRECTO DE IBAI"
    assert is_live is True


@pytest.mark.asyncio
async def test_extract_twitch_title_fallback():
    """If title is missing, fallback uses uploader or default 'Twitch Stream'."""
    fake_ytdlp = _install_fake_ytdlp()
    ydl = MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.extract_info.return_value = _twitch_info(title=None, uploader="ibai")
    fake_ytdlp.YoutubeDL.return_value = ydl

    from golive.ytdlp import _yt_extract_url

    _, title, _ = await _yt_extract_url("https://twitch.tv/ibai")
    assert title == "ibai"


def _normalize_stream_input(canal: str) -> tuple[str, str, str]:
    """Helper representing the normalization logic added to bot.py /stream command."""
    raw_canal = canal.strip()
    if not raw_canal.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        if re.match(r"^(?:www\.)?twitch\.tv/", raw_canal, re.I):
            canal = f"https://{raw_canal}"
        elif re.match(r"^twitch[:/ ]+([a-zA-Z0-9_]+)$", raw_canal, re.I):
            m = re.match(r"^twitch[:/ ]+([a-zA-Z0-9_]+)$", raw_canal, re.I)
            canal = f"https://www.twitch.tv/{m.group(1)}"

    is_url = canal.startswith(("http://", "https://", "rtsp://", "rtmp://"))

    if is_url:
        stream_url = canal
        if re.search(r'twitch\.tv', canal, re.I):
            source_type = "twitch"
            match = re.search(r'twitch\.tv/([a-zA-Z0-9_]+)', canal, re.I)
            channel_name = match.group(1) if match else "Twitch"
        elif re.search(r'youtube\.com|youtu\.be', canal, re.I):
            source_type = "youtube"
            channel_name = "YouTube Stream"
        else:
            source_type = "url"
            channel_name = "Stream Directo"
    else:
        source_type = "iptv"
        stream_url = canal
        channel_name = canal

    return stream_url, channel_name, source_type


@pytest.mark.parametrize(
    "input_canal, expected_url, expected_channel, expected_type",
    [
        (
            "https://www.twitch.tv/ibai?lang=es",
            "https://www.twitch.tv/ibai?lang=es",
            "ibai",
            "twitch",
        ),
        (
            "twitch.tv/ibai",
            "https://twitch.tv/ibai",
            "ibai",
            "twitch",
        ),
        (
            "twitch ibai",
            "https://www.twitch.tv/ibai",
            "ibai",
            "twitch",
        ),
        (
            "twitch:ibai",
            "https://www.twitch.tv/ibai",
            "ibai",
            "twitch",
        ),
        (
            "twitch/ibai",
            "https://www.twitch.tv/ibai",
            "ibai",
            "twitch",
        ),
    ],
)
def test_twitch_input_normalization(input_canal, expected_url, expected_channel, expected_type):
    url, ch_name, src_type = _normalize_stream_input(input_canal)
    assert url == expected_url
    assert ch_name == expected_channel
    assert src_type == expected_type
