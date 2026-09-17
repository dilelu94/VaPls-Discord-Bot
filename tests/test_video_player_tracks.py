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
    with patch.object(vp, "_ENCODER", dummy), \
         patch.object(vp, "_extract_subtitle_file", return_value="/tmp/mock_sub.ass"), \
         patch.object(vp, "_fetch_opensubtitles_file", return_value="/tmp/mock_sub.srt"), \
         patch("os.path.exists", return_value=True), \
         patch("os.path.getsize", return_value=100):
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
    assert "-filter_threads" in cmd_nosub
    ft_idx = cmd_nosub.index("-filter_threads")
    assert cmd_nosub[ft_idx + 1] == "2"

    # Subtitle track 1 selected
    p_sub = H264VideoPlayer("http://example.com/video.mkv", vc, subtitle_track=1)
    cmd_sub = p_sub._ffmpeg_cmd()
    assert "-vf" in cmd_sub
    vf_idx = cmd_sub.index("-vf")
    vf_str = cmd_sub[vf_idx + 1]
    assert "subtitles=f=" in vf_str
    assert "original_size=1280x720" in vf_str
    assert "fontsdir=" in vf_str

    # Explicit subtitle_file parameter
    with patch("os.path.exists", return_value=True), patch("os.path.getsize", return_value=100):
        p_subfile = H264VideoPlayer("http://example.com/video.mkv", vc, subtitle_file="/tmp/custom_sub.srt")
        cmd_subfile = p_subfile._ffmpeg_cmd()
        assert "-vf" in cmd_subfile
        vf_idx = cmd_subfile.index("-vf")
        vf_str = cmd_subfile[vf_idx + 1]
        assert "subtitles=f='/tmp/custom_sub.srt'" in vf_str
        assert "original_size=1280x720" in vf_str


def test_ffmpeg_cmd_start_time_multiple_inputs():
    vc = MagicMock()
    vc.ssrc = 100

    video_url = "https://googlevideo.com/videoplayback_video"
    audio_url = "https://googlevideo.com/videoplayback_audio"

    # Single URL with start_time
    player_single = H264VideoPlayer(video_url, vc, start_time=1729.0)
    cmd_single = player_single._ffmpeg_cmd()
    ss_indices_single = [i for i, arg in enumerate(cmd_single) if arg == "-ss"]
    assert len(ss_indices_single) == 1
    assert cmd_single[ss_indices_single[0] + 1] == "1729.0"
    assert cmd_single[ss_indices_single[0] + 2] == "-i"

    # Tuple of (video_url, audio_url) with start_time
    player_tuple = H264VideoPlayer((video_url, audio_url), vc, start_time=1729.0)
    cmd_tuple = player_tuple._ffmpeg_cmd()
    ss_indices = [i for i, arg in enumerate(cmd_tuple) if arg == "-ss"]
    assert len(ss_indices) == 2, f"-ss should be specified for both inputs in {cmd_tuple}"
    # Verify each -ss is followed by timestamp and then -i
    for idx in ss_indices:
        assert cmd_tuple[idx + 1] == "1729.0"
        assert cmd_tuple[idx + 2] == "-i"



