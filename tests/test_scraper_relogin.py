"""Behavior: when Instagram invalidates the scraper session (login_required),
process_inbox re-authenticates with the credentials from .scraper_env instead
of calling cl.relogin() — which fails because load_settings() never sets
cl.username/cl.password on the instagrapi client."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from instagrapi.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "instagram_scraper"))
import scraper


def test_process_inbox_relogins_with_env_password(monkeypatch):
    cl = MagicMock()
    cl.direct_threads.side_effect = ClientError("login_required")
    monkeypatch.setenv("INSTAGRAM_PASSWORD", "pw")

    scraper.process_inbox(cl)

    cl.login.assert_called_once_with(scraper.INSTAGRAM_USERNAME, "pw")
    cl.dump_settings.assert_called_once_with(scraper.COOKIES_PATH)


def test_relogin_falls_back_to_client_relogin_without_password(monkeypatch):
    cl = MagicMock()
    monkeypatch.delenv("INSTAGRAM_PASSWORD", raising=False)

    scraper._relogin(cl)

    cl.relogin.assert_called_once()
    cl.dump_settings.assert_called_once_with(scraper.COOKIES_PATH)
