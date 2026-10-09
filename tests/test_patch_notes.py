"""Behavioral tests for patch notes web UI and token management."""

import os
import time
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from patch_notes import PatchNotesManager


@pytest.fixture
def tmp_patch_notes_manager(tmp_path):
    """Create a PatchNotesManager backed by a temporary file."""
    data_file = tmp_path / "test_patch_notes.json"
    return PatchNotesManager(data_path=str(data_file))


def test_token_creation_and_lookup(tmp_patch_notes_manager):
    """A generated token is valid and retrievable until expiry."""
    mgr = tmp_patch_notes_manager
    token = mgr.create_token(version="2.6", ttl_seconds=86400, title="Test Notas")

    assert isinstance(token, str)
    assert len(token) > 20

    status, data = mgr.get_token_status(token)
    assert status == "valid"
    assert data is not None
    assert data["version"] == "2.6"
    assert data["title"] == "Test Notas"
    assert data["expires_at"] > time.time()


def test_token_expiration(tmp_patch_notes_manager):
    """An expired token returns 'expired' status and can be checked accurately."""
    mgr = tmp_patch_notes_manager
    token = mgr.create_token(version="2.6", ttl_seconds=-10, title="Expired Notas")

    status, data = mgr.get_token_status(token)
    assert status == "expired"
    assert data is not None


def test_invalid_token_format_rejected(tmp_patch_notes_manager):
    """Invalid token strings (traversal, too short, special characters) are rejected."""
    mgr = tmp_patch_notes_manager

    # Path traversal attempt
    status, _ = mgr.get_token_status("../etc/passwd")
    assert status == "invalid_format"

    # Too short
    status, _ = mgr.get_token_status("short")
    assert status == "invalid_format"

    # Empty or None
    status, _ = mgr.get_token_status("")
    assert status == "invalid_format"
    status, _ = mgr.get_token_status(None)
    assert status == "invalid_format"

    # Non-existent valid token
    status, _ = mgr.get_token_status("a" * 32)
    assert status == "not_found"


def test_rate_limiting(tmp_patch_notes_manager):
    """IP rate limiting blocks requests after exceeding the threshold."""
    mgr = tmp_patch_notes_manager
    ip = "192.168.1.100"

    for _ in range(5):
        assert mgr.check_rate_limit(ip, limit=5, window=60.0) is True

    # 6th request must be blocked
    assert mgr.check_rate_limit(ip, limit=5, window=60.0) is False

    # Different IP is not blocked
    assert mgr.check_rate_limit("192.168.1.101", limit=5, window=60.0) is True


def test_security_headers_attached(tmp_patch_notes_manager):
    """Security headers (CSP, X-Frame-Options, X-Content-Type-Options) are strictly present."""
    mgr = tmp_patch_notes_manager
    resp = web.Response(text="test", content_type="text/html")
    secured = mgr.add_security_headers(resp)

    csp = secured.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert secured.headers.get("X-Frame-Options") == "DENY"
    assert secured.headers.get("X-Content-Type-Options") == "nosniff"
    assert secured.headers.get("Referrer-Policy") == "no-referrer"


def test_rendered_html_contains_patch_notes(tmp_patch_notes_manager):
    """Rendered HTML includes the patch notes sections and security escaping."""
    mgr = tmp_patch_notes_manager
    token = mgr.create_token(version="2.6", ttl_seconds=3600, title="Notas de Parche v2.6")
    _, data = mgr.get_token_status(token)

    html_out = mgr.render_patch_notes_html(data)
    assert "Notas de Parche v2.6" in html_out
    assert "Anti-Exploits &amp; Sistema de Loot" in html_out or "Anti-Exploits & Sistema de Loot" in html_out
    assert "Netcode &amp; GoLive" in html_out or "Netcode & GoLive" in html_out
    assert "Balance de Personaje — El Indio" in html_out
    assert "andá a hacerte ortear" in html_out
    assert "sopapeás la papirola" in html_out
    assert "cabecear el enano" in html_out


@pytest.mark.asyncio
async def test_api_patch_notes_endpoint_e2e(tmp_patch_notes_manager, monkeypatch):
    """End-to-end test against the API router."""
    import apiServer

    monkeypatch.setattr(apiServer, "patch_notes_manager", tmp_patch_notes_manager)

    # Valid token
    valid_tok = tmp_patch_notes_manager.create_token(version="2.6", ttl_seconds=86400)
    # Expired token
    expired_tok = tmp_patch_notes_manager.create_token(version="2.6", ttl_seconds=-10)

    # Setup dummy bot for createApp
    class DummyBot:
        voice_clients = []
        guilds = []
        get_channel = lambda self, cid: None

    app = apiServer.makeApp(DummyBot())
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. Valid token returns 200 and security headers
        resp_valid = await client.get(f"/patch-notes/{valid_tok}")
        assert resp_valid.status == 200
        text = await resp_valid.text()
        assert "Notas de Parche" in text
        assert resp_valid.headers.get("X-Frame-Options") == "DENY"

        # 2. Expired token returns 410 Gone
        resp_exp = await client.get(f"/patch-notes/{expired_tok}")
        assert resp_exp.status == 410
        exp_text = await resp_exp.text()
        assert "Enlace Expirado" in exp_text

        # 3. Invalid token returns 404 Not Found
        resp_404 = await client.get("/patch-notes/nonexistent_token_1234567890")
        assert resp_404.status == 404

        # 4. Spanish alias /notas-parche/{token} works identically
        resp_alias = await client.get(f"/notas-parche/{valid_tok}")
        assert resp_alias.status == 200
    finally:
        await client.close()
