"""Unit tests for H264VideoPlayer seek() and current_position functionality."""

import threading
from unittest.mock import MagicMock, patch
import pytest

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
    with patch.object(vp, "_ENCODER", dummy):
        yield


def test_h264_video_player_current_position_initial():
    vc = MagicMock()
    vc.ssrc = 1000
    player = H264VideoPlayer(
        url="http://example.com/video.mp4",
        voice_client=vc,
        fps=25.0,
        start_time=120.0,
    )
    assert player.current_position == 120.0
    player._frames_emitted = 250  # 250 frames at 25 fps = 10 seconds
    assert player.current_position == 130.0


def test_h264_video_player_seek_updates_position_and_triggers_event():
    vc = MagicMock()
    vc.ssrc = 1000
    player = H264VideoPlayer(
        url="http://example.com/video.mp4",
        voice_client=vc,
        fps=25.0,
        start_time=0.0,
    )
    player._frames_emitted = 500

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    player._proc = mock_proc

    with patch.object(player, "_kill_proc") as mock_kill:
        player.seek(300.0)
        assert player._start_time == 300.0
        assert player._frames_emitted == 0
        assert player.current_position == 300.0
        assert player._seeking_event.is_set()
        mock_kill.assert_called_once_with(mock_proc)


def test_h264_video_player_ffmpeg_cmd_setpts_subtitles(tmp_path):
    sub_file = str(tmp_path / "test.srt")
    with open(sub_file, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")

    vc = MagicMock()
    vc.ssrc = 1000
    player = H264VideoPlayer(
        url="http://example.com/video.mp4",
        voice_client=vc,
        fps=25.0,
        start_time=128.8,
        subtitle_file=sub_file,
    )
    cmd = player._ffmpeg_cmd()
    cmd_str = " ".join(cmd)
    assert "setpts=PTS+128.800/TB" in cmd_str
    assert "subtitles=" in cmd_str
    assert "setpts=PTS-STARTPTS" in cmd_str

