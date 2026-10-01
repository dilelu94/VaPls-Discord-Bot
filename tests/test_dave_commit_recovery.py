"""
test_dave_commit_recovery.py – Verify DAVE commit/welcome rejection error handling.

Tests that process_commit and process_welcome raise exceptions when a commit or welcome
is rejected by libdave, allowing gateway.py to catch the exception and trigger
_recover_from_invalid_commit instead of silently falling back to unencrypted passthrough mode.
"""

from unittest.mock import MagicMock
import pytest

dave = pytest.importorskip("dave")
from davey_compat import DaveSession


def test_process_commit_raises_on_rejection():
    """Verify process_commit raises RuntimeError when libdave returns a RejectType."""
    session = DaveSession(protocol_version=1, user_id=123456, channel_id=789012)

    mock_impl = MagicMock()
    mock_impl.process_commit.return_value = dave.RejectType.ignored
    session._session = mock_impl

    with pytest.raises(RuntimeError, match="MLS commit rejected: ignored"):
        session.process_commit(b"dummy_commit_data")


def test_process_welcome_raises_on_rejection():
    """Verify process_welcome raises RuntimeError when libdave returns None (rejection)."""
    session = DaveSession(protocol_version=1, user_id=123456, channel_id=789012)

    mock_impl = MagicMock()
    mock_impl.process_welcome.return_value = None
    session._session = mock_impl

    with pytest.raises(RuntimeError, match="MLS welcome rejected by libdave"):
        session.process_welcome(b"dummy_welcome_data")
