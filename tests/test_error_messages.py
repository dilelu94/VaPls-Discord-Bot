"""Behavior: every Gemini failure kind maps to a non-empty, user-facing message,
and the "indio" persona speaks differently from the default "vapls" voice.

Assertions stay loose (non-empty, contains the HTTP status, indio != vapls) so
the actual wording can be reworded freely without breaking tests.
"""
import pytest

from geminiCommand import _error_message

KINDS = ["config", "timeout", "http", "blocked", "empty", "parse", "something-unknown"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("persona", ["vapls", "indio"])
def test_every_kind_returns_nonempty_message(kind, persona):
    msg = _error_message(kind, None, persona)
    assert isinstance(msg, str) and msg.strip()


@pytest.mark.parametrize("kind", ["timeout", "http", "blocked", "empty"])
def test_indio_voice_differs_from_vapls(kind):
    # These kinds have persona-specific phrasing.
    assert _error_message(kind, 500, "indio") != _error_message(kind, 500, "vapls")


def test_http_status_surfaced_in_message():
    assert "500" in _error_message("http", 500, "vapls")


def test_http_rate_limit_differs_from_generic_http():
    rate_limited = _error_message("http", 429, "vapls")
    generic = _error_message("http", 500, "vapls")
    assert rate_limited != generic


@pytest.mark.parametrize("persona", ["vapls", "indio"])
def test_service_unavailable_is_its_own_case(persona):
    # A 503 (Gemini overloaded / down) is an expected, transient outage — it
    # should read as "the service is down, try later", not leak a raw HTTP code
    # like the generic server-error path does.
    unavailable = _error_message("http", 503, persona)
    generic = _error_message("http", 500, persona)
    rate_limited = _error_message("http", 429, persona)
    assert unavailable.strip()
    assert unavailable != generic
    assert unavailable != rate_limited
    assert "503" not in unavailable


def test_service_unavailable_indio_voice_differs_from_vapls():
    assert _error_message("http", 503, "indio") != _error_message("http", 503, "vapls")


def test_config_message_shared_across_personas():
    # Config errors are an admin concern, same message for both voices.
    assert _error_message("config", None, "indio") == _error_message("config", None, "vapls")
