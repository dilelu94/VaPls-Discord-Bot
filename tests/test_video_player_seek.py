"""Unit tests for H264VideoPlayer seek() and current_position functionality."""

import threading
from unittest.mock import MagicMock, patch
import pytest

from golive.slopsoil.video_player import H264VideoPlayer


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
