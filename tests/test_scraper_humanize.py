"""Behavior: the scraper humanizes its fingerprint (per-request jitter via
delay_range, read+type pauses before sends) and backs off instead of relogging
when Instagram raises a ChallengeRequired checkpoint."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from instagrapi.exceptions import ChallengeRequired, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "instagram_scraper"))
import scraper


@pytest.fixture
def backoff_path(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "LOGIN_BACKOFF_PATH", str(tmp_path / "login_backoff.json"))
    return scraper.LOGIN_BACKOFF_PATH


def test_human_delay_sleeps_shortly():
    t0 = time.time()
    scraper._human_delay(0, 0.1, "")
    assert time.time() - t0 < 1


def test_login_backoff_record_clear(backoff_path):
    assert scraper.login_blocked_until() is None
    scraper.record_login_failure("challenge")
    assert scraper.login_blocked_until() is not None
    scraper.clear_login_backoff()
    assert scraper.login_blocked_until() is None


def test_login_backoff_long_after_repeated_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "LOGIN_BACKOFF_PATH", str(tmp_path / "login_backoff.json"))
    monkeypatch.setattr(scraper, "LONG_BACKOFF_SECS", 7 * 86400)
    monkeypatch.setattr(scraper, "CHALLENGE_BACKOFF_SECS", 3600)
    monkeypatch.setattr(scraper, "REPEATED_FAILURES_LIMIT", 3)
    for _ in range(3):
        scraper.record_login_failure("challenge")
    state = scraper.load_login_backoff()
    assert state["failures"] == 3
    assert state["next_allowed_at"] - time.time() >= 6 * 86400


def test_process_inbox_challenge_does_not_relogin(backoff_path):
    cl = MagicMock()
    cl.direct_threads.side_effect = ChallengeRequired("challenge_required")
    scraper.process_inbox(cl)
    cl.login.assert_not_called()
    cl.relogin.assert_not_called()
    assert scraper.load_login_backoff()["last_reason"] == "challenge"


def test_process_inbox_login_required_relogins(backoff_path, monkeypatch):
    cl = MagicMock()
    cl.direct_threads.side_effect = ClientError("login_required")
    monkeypatch.setenv("INSTAGRAM_PASSWORD", "pw")
    scraper.process_inbox(cl)
    cl.login.assert_called_once_with(scraper.INSTAGRAM_USERNAME, "pw")


def test_process_inbox_clears_backoff_on_success(backoff_path):
    scraper.save_login_backoff({"failures": 1, "next_allowed_at": time.time() + 3600})
    cl = MagicMock()
    cl.direct_threads.return_value = []
    cl.user_id = "1"
    scraper.process_inbox(cl)
    assert scraper.load_login_backoff() == {}
