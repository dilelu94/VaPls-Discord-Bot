"""Tests for DAVE dynamic audio SSRC resolution and voice websocket reconnects."""

from unittest.mock import AsyncMock, MagicMock
import pytest

dave = pytest.importorskip("dave")
from davey_compat import DaveSession


def test_dave_session_defaults_to_fallback_ssrc():
    session = DaveSession(protocol_version=1, user_id=123456, channel_id=789012)
    assert session._get_audio_ssrc() == session._SSRC
    assert session._assigned_audio_ssrc == session._SSRC


def test_dave_session_resolves_dynamic_voice_client_ssrc():
    session = DaveSession(protocol_version=1, user_id=123456, channel_id=789012)

    mock_voice_state = MagicMock()
    mock_vc = MagicMock()
    mock_vc.ssrc = 4198
    mock_voice_state.voice_client = mock_vc
    session._voice_state = mock_voice_state

    mock_encryptor = MagicMock()
    mock_encryptor.encrypt.return_value = b"encrypted_opus_data"
    session._encryptor = mock_encryptor

    result = session.encrypt_opus(b"raw_opus_frame")

    assert result == b"encrypted_opus_data"
    mock_encryptor.assign_ssrc_to_codec.assert_called_with(4198, dave.Codec.opus)
    mock_encryptor.encrypt.assert_called_with(dave.MediaType.audio, 4198, b"raw_opus_frame")
    assert session._assigned_audio_ssrc == 4198


def test_dave_session_reset_and_reinit_resets_audio_ssrc():
    session = DaveSession(protocol_version=1, user_id=123456, channel_id=789012)
    session._assigned_audio_ssrc = 9999

    session.reset()
    assert session._assigned_audio_ssrc == session._SSRC

    session._assigned_audio_ssrc = 8888
    session.reinit(protocol_version=1, user_id=123456, channel_id=789012)
    assert session._assigned_audio_ssrc == session._SSRC
