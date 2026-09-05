import sys
from unittest.mock import MagicMock
import pytest

# Ensure mocks for missing optional dependencies before importing video_player
for _mod in ("video_compat", "davey_compat", "golive_connection", "golive.slopsoil.golive", "ytdlp"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import discord
if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

from unittest.mock import patch
from golive.slopsoil.video_player import H264VideoPlayer, _EncoderConfig
import golive.slopsoil.video_player as vp


@pytest.fixture(autouse=True)
def mock_encoder():
    dummy = _EncoderConfig(
        name="libx264",
        pre_input=[],
        post_codec=["-preset", "ultrafast"],
        vf="scale=1280x720,format=yuv420p",
    )
    with patch.object(vp, "_ENCODER", dummy), patch.object(vp, "_extract_subtitle_file", return_value="/tmp/mock_sub.ass"):
        yield


def test_ffmpeg_cmd_audio_track_mapping():
    vc = MagicMock()
    vc.ssrc = 100

    # Default audio track (0)
    p0 = H264VideoPlayer("http://example.com/video.mkv", vc, audio_track=0)
    cmd0 = p0._ffmpeg_cmd()
    assert "0:a:0?" in cmd0

    # Selected audio track 2
    p2 = H264VideoPlayer("http://example.com/video.mkv", vc, audio_track=2)
    cmd2 = p2._ffmpeg_cmd()
    assert "0:a:2?" in cmd2


def test_ffmpeg_cmd_subtitle_burn_in_filter():
    vc = MagicMock()
    vc.ssrc = 100

    # No subtitles (-1)
    p_nosub = H264VideoPlayer("http://example.com/video.mkv", vc, subtitle_track=-1)
    cmd_nosub = p_nosub._ffmpeg_cmd()
    vf_idx = cmd_nosub.index("-vf")
    assert "subtitles=" not in cmd_nosub[vf_idx + 1]

    # Subtitle track 1 selected
    p_sub = H264VideoPlayer("http://example.com/video.mkv", vc, subtitle_track=1)
    cmd_sub = p_sub._ffmpeg_cmd()
    assert "-vf" in cmd_sub
    vf_idx = cmd_sub.index("-vf")
    vf_str = cmd_sub[vf_idx + 1]
    assert "subtitles=f=" in vf_str

    # Explicit subtitle_file parameter
    with patch("os.path.exists", return_value=True), patch("os.path.getsize", return_value=100):
        p_subfile = H264VideoPlayer("http://example.com/video.mkv", vc, subtitle_file="/tmp/custom_sub.srt")
        cmd_subfile = p_subfile._ffmpeg_cmd()
        assert "-vf" in cmd_subfile
        vf_idx = cmd_subfile.index("-vf")
        vf_str = cmd_subfile[vf_idx + 1]
        assert "subtitles=f='/tmp/custom_sub.srt'" in vf_str


