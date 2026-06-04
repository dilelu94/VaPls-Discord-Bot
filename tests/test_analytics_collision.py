import pytest
import analytics
from unittest.mock import patch

def test_capture_prevents_user_id_collision():
    # If properties contains 'user_id', it should not crash the 'capture' call
    # when passed to track_request which also has a 'user_id' argument.
    with patch("posthog_client.track_request") as mock_track:
        analytics.capture("test_event", properties={"user_id": "colliding_id", "foo": "bar"})
        
        mock_track.assert_called_once()
        _, _, kwargs = mock_track.mock_calls[0]
        # It should either rename the key or have handled the collision
        assert "foo" in kwargs
        # If renamed, it should be something else
        assert kwargs.get("user_id") != "colliding_id"

def test_capture_exception_prevents_user_id_collision():
    with patch("posthog_client.capture_error") as mock_error:
        analytics.capture_exception(ValueError("boom"), properties={"user_id": "colliding_id"})
        
        mock_error.assert_called_once()
        # capture_error(error, user_id=did, **props)
        # If properties had user_id, it would crash if not handled
