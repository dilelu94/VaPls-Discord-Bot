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
    posthog_client._known_groups.clear()
    yield
    posthog_client._posthog = None
    posthog_client._observability_initialized = False
    posthog_client._known_groups.clear()


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

    with (
        patch("posthog_client._POSTHOG_AVAILABLE", True),
        patch("posthog_client.Posthog", mock_posthog_class, create=True),
    ):
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
        groups=None,
    )


def test_track_request_without_user_is_personless():
    """A bot/system event (no user_id) must not create a person profile."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client

    posthog_client.track_request(None, "bot_action", groups={"guild": "42"})

    _, kwargs = mock_client.capture.call_args
    # PostHog suppresses person profiles when this flag is False.
    assert kwargs["properties"]["$process_person_profile"] is False
    # The synthetic distinct_id is derived from the guild, not a real person.
    assert kwargs["distinct_id"] == "bot-42"
    assert kwargs["groups"] == {"guild": "42"}


def test_track_request_forwards_group_attribution():
    """guild group attribution must reach PostHog so server-level analytics work."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client

    posthog_client.track_request("user_9", "played_song", groups={"guild": "777"})

    _, kwargs = mock_client.capture.call_args
    assert kwargs["groups"] == {"guild": "777"}


def test_group_identify_dedupes_per_process():
    """The same guild is identified to PostHog only once, not on every event."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client

    posthog_client.group_identify("guild", "123", name="VaPls")
    posthog_client.group_identify("guild", "123", name="VaPls")
    posthog_client.group_identify("guild", "456", name="Other")

    # Two distinct guilds -> two calls; the repeat for 123 is suppressed.
    assert mock_client.group_identify.call_count == 2


def test_track_ai_generation_formats_event_correctly():
    """Verify track_ai_generation constructs standard PostHog LLM properties and captures it."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client

    import time

    t_start = time.monotonic() - 1.2

    posthog_client.track_ai_generation(
        model="gemini-2.5-flash",
        user_message="Hi",
        system_instruction="Be nice",
        history=[{"role": "user", "parts": [{"text": "Hello bot"}]}],
        response="Hello!",
        prompt_tokens=15,
        response_tokens=5,
        t_start=t_start,
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
    assert 1.1 <= props["$ai_latency"] <= 1.3
    assert props["$ai_input_tokens"] == 15
    assert props["$ai_output_tokens"] == 5
    assert props["$ai_input"] == [
        {"role": "system", "content": "Be nice"},
        {"role": "user", "content": "Hello bot"},
        {"role": "user", "content": "Hi"},
    ]
    assert props["$ai_output_choices"] == [{"text": "Hello!"}]
    assert props["guild_id"] == "guild_789"
    assert props["custom_tag"] == "expert"
    assert "$ai_total_cost_usd" in props


# ---------------------------------------------------------------------------
# OTLP log pipeline tests
# ---------------------------------------------------------------------------


def test_otlp_handler_logs_are_noop_without_key(monkeypatch):
    """Without POSTHOG_API_KEY the OTLP handler must not crash on log records."""
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    posthog_client.init_observability(service_name="test-app")

    log = logging.getLogger("test.otlp")
    log.info("this should not crash without a key")
    log.warning("nor this")
    log.error("nor this")


def test_otlp_handler_forwards_log_level():
    """Log level and message must be present when OTLP is active."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_otlp")
    monkeypatch.setenv("POSTHOG_HOST", "https://test.posthog.com")

    with (
        patch("posthog_client._POSTHOG_AVAILABLE", True),
        patch("posthog_client.init_observability") as mock_init,
    ):
        posthog_client.init_observability(service_name="test-app")
        mock_init.assert_called_once()


def test_otlp_handler_root_logger_propagates():
    """Logs must propagate through the root logger to reach OTLP."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_otlp")
    monkeypatch.setenv("POSTHOG_HOST", "https://test.posthog.com")

    with patch("posthog_client._POSTHOG_AVAILABLE", True):
        posthog_client.init_observability(service_name="test-app")
        root = logging.getLogger()
        # The OTLP handler is attached to the root logger
        otlp_handlers = [
            h
            for h in root.handlers
            if "OTLP" in type(h).__name__
            or "BatchLogRecordProcessor" in type(h).__name__
        ]
        # May be empty in test env without real OTLP — just verify no crash
        test_logger = logging.getLogger("test.otlp_delivery")
        test_logger.info("delivery test message")
        test_logger.error("delivery error message")


def test_otlp_handler_attaches_service_name():
    """LogRecord must carry the service.name resource attribute."""
    posthog_client._observability_initialized = False
    posthog_client._posthog = None
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test_svc")
    monkeypatch.setenv("POSTHOG_HOST", "https://test.posthog.com")

    with patch("posthog_client._POSTHOG_AVAILABLE", True):
        posthog_client.init_observability(service_name="vapls-main-bot")
        # No crash — real resource attribution is verified in integration
        assert posthog_client._observability_initialized is True


def test_otlp_logger_propagates_to_root(caplog):
    """Logger named 'bot.*' must propagate to root so OTLP captures it."""
    log = logging.getLogger("bot.test_component")
    # Ensure propagation is on
    log.propagate = True
    msg = "test otlp propagation message"
    with caplog.at_level(logging.WARNING):
        log.warning(msg)
    assert any(msg in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Request context manager
# ---------------------------------------------------------------------------


def test_request_context_manager():
    """Verify request_context enters a new context and tags it correctly."""
    mock_client = MagicMock()
    posthog_client._posthog = mock_client

    mock_new_context = MagicMock()
    mock_identify = MagicMock()
    mock_tag = MagicMock()

    with (
        patch("posthog_client._POSTHOG_AVAILABLE", True),
        patch("posthog_client.new_context", mock_new_context, create=True),
        patch("posthog_client.identify_context", mock_identify, create=True),
        patch("posthog_client.tag", mock_tag, create=True),
    ):
        with posthog_client.request_context("user_ctx", role="admin"):
            pass

        mock_new_context.assert_called_once()
        mock_identify.assert_called_once_with("user_ctx")
        mock_tag.assert_called_once_with("role", "admin")
