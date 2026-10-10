"""Behavioral tests for public landing page and command catalog at /."""

from unittest.mock import MagicMock
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import apiServer
from landing_page import LandingPageManager, landing_page_manager
from stremio_sessions import session_manager


@pytest.fixture
def dummy_bot():
    bot = MagicMock()
    bot.is_ready.return_value = True
    bot.guilds = []
    bot.voice_clients = []
    bot.get_channel = lambda cid: None
    return bot


def test_landing_page_html_rendered():
    """HTML contains all key command sections, security notice, and Discord simulation modal."""
    mgr = LandingPageManager()
    html_out = mgr.render_landing_page_html()

    # Title & SEO
    assert "VaPls &amp; El Indio" in html_out or "VaPls & El Indio" in html_out
    assert "Centro de Control y Comandos de Discord" in html_out

    # Key modules and slash commands
    assert "/stream" in html_out
    assert "/stopstream" in html_out
    assert "/play" in html_out
    assert "/parar" in html_out
    assert "/quit" in html_out
    assert "/clip" in html_out
    assert "/indio" in html_out
    assert "/vapls" in html_out
    assert "/entraindio" in html_out
    assert "/sensibilidad" in html_out
    assert "/soundpad" in html_out
    assert "/mascota" in html_out
    assert "/adivinador" in html_out
    assert "/transferir" in html_out
    assert "/sugerencias" in html_out
    assert "/israel-alerts" in html_out

    # Modal and Discord redirection elements
    assert "command-modal" in html_out
    assert "Ejecución Exclusiva en Discord" in html_out
    assert "btn-copy-command" in html_out
    assert "copyCommandToClipboard" in html_out
    assert "modal-command-code" in html_out
    assert "triggerCommand" in html_out

    # Stremio, torrent movies & anime emphasis
    assert "Stremio" in html_out
    assert "Anime" in html_out
    assert "Torrents" in html_out or "Torrent" in html_out

    # Verify web Discord link (discord.com/app) is completely removed
    assert "discord.com/app" not in html_out


def test_security_headers_attached():
    """Strict security headers are attached to the response."""
    mgr = LandingPageManager()
    resp = web.Response(text="test", content_type="text/html")
    secured = mgr.add_security_headers(resp)

    csp = secured.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert secured.headers.get("X-Frame-Options") == "DENY"
    assert secured.headers.get("X-Content-Type-Options") == "nosniff"
    assert secured.headers.get("Referrer-Policy") == "no-referrer"
    assert secured.headers.get("Cross-Origin-Opener-Policy") == "same-origin"


def test_zero_leaks_in_landing_page():
    """HTML page must strictly never leak sensitive credentials, tokens, or private IP addresses."""
    mgr = LandingPageManager()
    html_out = mgr.render_landing_page_html()

    # Forbidden leak strings
    forbidden_tokens = [
        "8934d374-90a5-4ca1-80de-91325be8666b",  # user duckdns token
        "DUCKDNS_TOKEN",
        "DISCORD_TOKEN",
        "API_SECRET",
        "GOLIVE_USER_TOKEN",
        "USERBOT_TOKEN",
        "TORBOX_TOKEN",
        "181.117.161.180",
        "141.148.84.55",
        "193.122.210.127",
        "129.80.59.99",
    ]
    for token in forbidden_tokens:
        assert token not in html_out, f"Sensitive leak detected: {token}"


def test_landing_page_rate_limiting():
    """Rate limiter restricts excessive requests from the same IP."""
    mgr = LandingPageManager()
    ip = "10.0.0.99"
    limit = 5

    for _ in range(limit):
        assert mgr.check_rate_limit(ip, limit=limit, window=60.0) is True

    # Limit exceeded
    assert mgr.check_rate_limit(ip, limit=limit, window=60.0) is False

    # Different IP is not throttled
    assert mgr.check_rate_limit("10.0.0.100", limit=limit, window=60.0) is True


@pytest.mark.asyncio
async def test_landing_page_e2e_endpoint(dummy_bot):
    """GET / without authentication serves the landing page with 200 OK and security headers."""
    app = apiServer.makeApp(dummy_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. GET / unauthenticated returns 200 OK HTML
        resp = await client.get("/")
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "text/html; charset=utf-8"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

        body = await resp.text()
        assert "VaPls &amp; El Indio" in body or "VaPls & El Indio" in body
        assert "/play" in body
        assert "/stream" in body
        assert "Ejecución Exclusiva en Discord" in body

        # 2. GET /index.html also returns 200 OK
        resp_index = await client.get("/index.html")
        assert resp_index.status == 200

        # 3. Rate limiting kicks in when limit exceeded
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(landing_page_manager, "check_rate_limit", lambda ip: False)
            resp_429 = await client.get("/")
            assert resp_429.status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_landing_page_delegates_to_stremio_when_token_provided(dummy_bot):
    """When a 32-char Stremio token is in the query params, GET / delegates to stremioIndex."""
    sess = session_manager.create_session(
        author_id=123,
        author_name="Tester",
        channel_id=456,
        guild_id=789,
    )
    token = sess.token

    app = apiServer.makeApp(dummy_bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # GET /?token=<valid_token> delegates to stremioIndex and serves Stremio UI
        resp = await client.get(f"/?token={token}")
        assert resp.status == 200
        text = await resp.text()
        assert "VaPls Stremio" in text

        # GET /?token=<invalid_hex_token> returns 403 Access Denied from stremioIndex
        fake_token = "0" * 32
        resp_fake = await client.get(f"/?token={fake_token}")
        assert resp_fake.status == 403
        fake_text = await resp_fake.text()
        assert "Acceso Denegado" in fake_text
    finally:
        await client.close()
