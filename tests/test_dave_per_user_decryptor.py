"""
test_dave_per_user_decryptor.py - Tests for isolated per-user/per-stream DAVE decryptors
and epoch-aware key ratchet transitions.
"""

from unittest.mock import MagicMock
import pytest

dave = pytest.importorskip("dave")
from davey_compat import DaveSession


def test_dave_session_creates_isolated_decryptor_per_user():
    """Verify that each user receives an independent dave.Decryptor instance."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)

    mock_impl = MagicMock()
    mock_impl.get_key_ratchet.return_value = None
    session._session = mock_impl

    raw_frame = b"\x01\x02\x03"
    session.decrypt(111, "audio", raw_frame)
    session.decrypt(222, "audio", raw_frame)

    assert "111" in session._audio_decryptors
    assert "222" in session._audio_decryptors
    assert session._audio_decryptors["111"] is not session._audio_decryptors["222"]


def test_dave_session_video_and_audio_decryptors_are_isolated():
    """Verify that video and audio decryptors for the same user are isolated."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)

    mock_impl = MagicMock()
    mock_impl.get_key_ratchet.return_value = None
    session._session = mock_impl

    raw_frame = b"\x01\x02\x03"
    session.decrypt(111, "audio", raw_frame)
    session.decrypt(111, "video", raw_frame)

    assert "111" in session._audio_decryptors
    assert "111" in session._video_decryptors
    assert session._audio_decryptors["111"] is not session._video_decryptors["111"]


def test_dave_session_epoch_transition_refreshes_existing_decryptors():
    """Verify that MLS commit epoch transitions update key ratchets for active user decryptors."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)
    session._encryptor = MagicMock()
    session._decryptor = MagicMock()

    mock_dec111 = MagicMock()
    mock_dec111.decrypt.return_value = b"decrypted_audio"
    session._audio_decryptors["111"] = mock_dec111
    session._audio_decryptor_epochs["111"] = 100
    session._epoch = 100

    mock_impl = MagicMock()
    ratchet_epoch2 = MagicMock()
    mock_impl.get_key_ratchet.return_value = ratchet_epoch2
    mock_impl.process_commit.return_value = {200: [111]}
    session._session = mock_impl

    # Advance MLS epoch via process_commit
    session.process_commit(b"dummy_commit")

    assert session._epoch == 200
    assert session._audio_decryptor_epochs["111"] == 200
    mock_dec111.transition_to_key_ratchet.assert_called_with(ratchet_epoch2)
    mock_dec111.transition_to_passthrough_mode.assert_called_with(False)


def test_dave_session_reset_and_reinit_clears_per_user_decryptors():
    """Verify that reset and reinit clear active decryptor maps."""
    session = DaveSession(protocol_version=1, user_id=999, channel_id=123)
    session._audio_decryptors["111"] = MagicMock()
    session._video_decryptors["111"] = MagicMock()

    session.reset()
    assert len(session._audio_decryptors) == 0
    assert len(session._video_decryptors) == 0

    session._audio_decryptors["222"] = MagicMock()
    session.reinit(protocol_version=1, user_id=999, channel_id=123)
    assert len(session._audio_decryptors) == 0
    assert len(session._video_decryptors) == 0
