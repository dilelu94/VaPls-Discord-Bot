"""Behavior: posthog_client.py provides safe wrappers for PostHog and OpenTelemetry.

When POSTHOG_API_KEY is not configured or modules fail to import, all
observability functions degrade gracefully and act as silent no-ops.
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
import posthog_client


@pytest.fixture(autouse=True)
def reset_client_globals():
    """Ensure we reset the initialized states of posthog_client before and after tests."""
    posthog_client._posthog = None
    posthog_client._observability_initialized = False
    yield
    posthog_client._posthog = None
    posthog_client._observability_initialized = False


def test_init_observability_no_key_is_noop(monkeypatch):
    """Verify that if POSTHOG_API_KEY is missing, no PostHog instance is created."""
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    
    posthog_client.init_observability(service_name="test-app")
    
    assert posthog_client._observability_initialized is True
    assert posthog_client._posthog is None


def test_init_observability_with_key_creates_client(monkeypatch):
    """Verify that if POSTHOG_API_KEY is present, PostHog is initialized."""
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_key_123")
    monkeypatch.setenv("POSTHOG_HOST", "https://test.posthog.com")
    
    # Mock Posthog class to avoid real network configuration
    mock_posthog_class = MagicMock()
    
    with patch("posthog_client._POSTHOG_AVAILABLE", True), \
         patch("posthog_client.Posthog", mock_posthog_class, create=True):
        posthog_client.init_observability(service_name="test-app")
        
    assert posthog_client._observability_initialized is True
    mock_posthog_class.assert_called_once_with(
        project_api_key="phc_test_key_123",
        host="https://test.posthog.com",
        enable_exception_autocapture=True,
        capture_exception_code_variables=False,
    )


def test_track_request_noop_when_not_initialized():
    """Verify track_request is safe when not initialized (does not throw)."""
    assert posthog_client._posthog is None
    # This should not raise any exceptions
    posthog_client.track_request("user1", "test_event", custom_prop="value")


def test_track_request_delegates_to_posthog():
    """Verify track_request delegates the capture call to the internal Posthog client."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client
    
    posthog_client.track_request("user_123", "button_clicked", source="web")
    
    mock_client.capture.assert_called_once_with(
        distinct_id="user_123",
        event="button_clicked",
        properties={"source": "web"},
    )


def test_track_ai_generation_formats_event_correctly():
    """Verify track_ai_generation constructs standard PostHog LLM properties and captures it."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client
    
    prompt = [{"role": "system", "content": "Be nice"}, {"role": "user", "content": "Hi"}]
    
    posthog_client.track_ai_generation(
        model="gemini-2.5-flash",
        prompt=prompt,
        response="Hello!",
        prompt_tokens=15,
        response_tokens=5,
        latency_sec=1.2,
        user_id="user_456",
        guild_id="guild_789",
        custom_tag="expert",
    )
    
    mock_client.capture.assert_called_once()
    args, kwargs = mock_client.capture.call_args
    
    assert kwargs["distinct_id"] == "user_456"
    assert kwargs["event"] == "$ai_generation"
    
    props = kwargs["properties"]
    assert props["$ai_model"] == "gemini-2.5-flash"
    assert props["$ai_latency"] == 1.2
    assert props["$ai_input_tokens"] == 15
    assert props["$ai_output_tokens"] == 5
    assert props["$ai_input"] == prompt
    assert props["$ai_output_choices"] == [{"text": "Hello!"}]
    assert props["guild_id"] == "guild_789"
    assert props["custom_tag"] == "expert"
    assert "$ai_total_cost_usd" in props


def test_request_context_manager():
    """Verify request_context enters a new context and tags it correctly."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client
    
    mock_new_context = MagicMock()
    mock_identify = MagicMock()
    mock_tag = MagicMock()
    
    with patch("posthog_client._POSTHOG_AVAILABLE", True), \
         patch("posthog_client.new_context", mock_new_context, create=True), \
         patch("posthog_client.identify_context", mock_identify, create=True), \
         patch("posthog_client.tag", mock_tag, create=True):
             
        with posthog_client.request_context("user_ctx", role="admin"):
            pass
            
        mock_new_context.assert_called_once()
        mock_identify.assert_called_once_with("user_ctx")
        mock_tag.assert_called_once_with("role", "admin")
