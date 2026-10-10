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
    assert data.get("expires_at") is None or data["expires_at"] > time.time()


def test_token_never_expires(tmp_patch_notes_manager):
    """Patch notes tokens are permanent and never expire, returning 'valid' status even with past timestamp."""
    mgr = tmp_patch_notes_manager
    token = mgr.create_token(version="2.6", ttl_seconds=-10, title="Permanent Notas")

    status, data = mgr.get_token_status(token)
    assert status == "valid"
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
    assert any(term in html_out for term in ("Anti-Exploits", "Saludos Raros"))
    assert any(term in html_out for term in ("Netcode", "GoLive"))
    assert any(term in html_out for term in ("Balance de Personaje", "Respuestas y Comportamiento", "El Indio"))
    assert "andá a hacerte ortear" in html_out
    assert "sopapeás la papirola" in html_out
    assert "cabecear el enano" in html_out


def test_dynamic_sections_rendering(tmp_patch_notes_manager):
    """Dynamic sections provided in token are rendered properly with escaping."""
    mgr = tmp_patch_notes_manager
    sections = [
        {
            "icon": "🚀",
            "title": "Novedades Semanales",
            "items": [
                {
                    "tag": "Buff",
                    "header": "Super Cambio",
                    "desc": "Detalle del cambio <script>alert(1)</script>",
                    "dialogues": ["probando diálogo"],
                }
            ],
        }
    ]
    token = mgr.create_token(version="3.0", title="Notas v3.0", sections=sections)
    _, data = mgr.get_token_status(token)
    html_out = mgr.render_patch_notes_html(data)

    assert "Notas v3.0" in html_out
    assert "Novedades Semanales" in html_out
    assert "Super Cambio" in html_out
    assert "tag-buff" in html_out
    assert "probando diálogo" in html_out
    # Script tag from user payload must be escaped
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out


def test_get_patch_notes_url():
    """URL generator outputs domain URL pointing to patch notes token."""
    from patch_notes import get_patch_notes_url

    url = get_patch_notes_url("test_token_1234567890")
    assert "patch-notes/test_token_1234567890" in url
    assert "vapls.duckdns.org" in url


def test_get_patch_notes_history_url():
    """History URL generator outputs public URL for the patch notes archive."""
    from patch_notes import get_patch_notes_history_url

    url = get_patch_notes_history_url()
    assert url.endswith("/patch-notes")
    assert "vapls.duckdns.org" in url


def test_history_archive_persistence(tmp_path):
    """Patch notes history archives entries and persists them across manager reloads."""
    data_file = str(tmp_path / "tokens.json")
    hist_file = str(tmp_path / "history.json")
    mgr = PatchNotesManager(data_path=data_file, history_path=hist_file)

    # 1. Initially empty
    assert len(mgr.get_history()) == 0

    # 2. Save entry
    entry = {
        "version": "2.8",
        "title": "Notas v2.8",
        "sections": [
            {
                "icon": "✨",
                "title": "Novedades",
                "items": [{"tag": "Buff", "header": "Cambio A", "desc": "Detalle A"}],
            }
        ],
    }
    mgr.save_to_history(entry)
    assert len(mgr.get_history()) == 1
    assert mgr.get_history()[0]["version"] == "2.8"

    # 3. Reload in new instance
    mgr2 = PatchNotesManager(data_path=data_file, history_path=hist_file)
    hist2 = mgr2.get_history()
    assert len(hist2) == 1
    assert hist2[0]["title"] == "Notas v2.8"

    # 4. Lookup entry
    found = mgr2.get_history_entry("2.8")
    assert found is not None
    assert found["title"] == "Notas v2.8"


def test_render_history_html_escapes_xss(tmp_patch_notes_manager):
    """Rendered history HTML neutralizes XSS payloads in version, title, desc, and dialogues."""
    mgr = tmp_patch_notes_manager
    entry = {
        "version": "3.0<script>alert('ver')</script>",
        "title": "Notas con XSS <script>alert('title')</script>",
        "sections": [
            {
                "icon": "⚠️",
                "title": "Sección Segura <img src=x onerror=alert(1)>",
                "items": [
                    {
                        "tag": "Fix",
                        "header": "Header Peligroso <svg onload=alert(2)>",
                        "desc": "Descripción Peligrosa <script>alert(3)</script>",
                        "dialogues": ["Diálogo peligroso <script>alert(4)</script>"],
                    }
                ],
            }
        ],
    }
    mgr.save_to_history(entry)
    html_out = mgr.render_history_html()

    # Raw XSS payload strings must not be present
    assert "<script>alert('ver')</script>" not in html_out
    assert "<script>alert('title')</script>" not in html_out
    assert "<img src=x onerror=alert(1)>" not in html_out
    assert "<svg onload=alert(2)>" not in html_out
    assert "<script>alert(3)</script>" not in html_out
    assert "<script>alert(4)</script>" not in html_out

    # Escaped safe entities must be present
    assert "&lt;script&gt;alert(&#x27;ver&#x27;)&lt;/script&gt;" in html_out or "&lt;script&gt;" in html_out
    assert "Historial de Notas de Parche" in html_out


def test_landing_page_includes_patch_notes_module():
    """The landing page exposes a patch notes navbar button, filter pill, and module card."""
    from landing_page import landing_page_manager

    landing_html = landing_page_manager.render_landing_page_html()
    assert "/patch-notes" in landing_html
    assert "Notas de Parche" in landing_html
    assert 'id="nav-patch-notes"' in landing_html
    assert 'id="card-patch-notes"' in landing_html
    assert 'id="pill-patches"' in landing_html


@pytest.mark.asyncio
async def test_api_patch_notes_endpoint_e2e(tmp_patch_notes_manager, monkeypatch):
    """End-to-end test against the API router."""
    import apiServer

    monkeypatch.setattr(apiServer, "patch_notes_manager", tmp_patch_notes_manager)

    # Valid token
    valid_tok = tmp_patch_notes_manager.create_token(version="2.6", ttl_seconds=86400)
    # Expired token
    expired_tok = tmp_patch_notes_manager.create_token(version="2.6", ttl_seconds=-10, save_history=False)

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

        # 2. Token created with past timestamp remains valid permanently (returns 200)
        resp_exp = await client.get(f"/patch-notes/{expired_tok}")
        assert resp_exp.status == 200
        exp_text = await resp_exp.text()
        assert "Notas de Parche" in exp_text

        # 3. Invalid token returns 404 Not Found
        resp_404 = await client.get("/patch-notes/nonexistent_token_1234567890")
        assert resp_404.status == 404

        # 4. Spanish alias /notas-parche/{token} works identically
        resp_alias = await client.get(f"/notas-parche/{valid_tok}")
        assert resp_alias.status == 200

        # 5. Historical archive at /patch-notes returns 200 with security headers
        resp_hist = await client.get("/patch-notes")
        assert resp_hist.status == 200
        hist_text = await resp_hist.text()
        assert "Historial de Notas de Parche" in hist_text
        assert resp_hist.headers.get("X-Frame-Options") == "DENY"
        assert resp_hist.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in resp_hist.headers

        # 6. Spanish alias /notas-parche returns 200
        resp_hist_es = await client.get("/notas-parche")
        assert resp_hist_es.status == 200

        # 7. Explicit subpath /patch-notes/historial returns 200
        resp_hist_sub = await client.get("/patch-notes/historial")
        assert resp_hist_sub.status == 200
    finally:
        await client.close()


def test_token_without_sections_seeds_into_history_with_default_sections(tmp_path):
    """Tokens stored without explicit sections (e.g. legacy token v2.6) are automatically
    seeded into history with structured sections and active link."""
    import json
    data_file = tmp_path / "tokens.json"
    hist_file = tmp_path / "history.json"
    data_file.write_text(json.dumps({
        "XN_zExXtxu8UZWaB7pbZD1ThvmUXLNoo5lnusFX8o0E": {
            "token": "XN_zExXtxu8UZWaB7pbZD1ThvmUXLNoo5lnusFX8o0E",
            "version": "2.6",
            "title": "Notas de Parche v2.6",
            "created_at": 1791572630.0,
            "expires_at": time.time() + 3600.0,
        }
    }), encoding="utf-8")

    mgr = PatchNotesManager(data_path=str(data_file), history_path=str(hist_file))
    history = mgr.get_history()
    assert len(history) >= 1
    v26 = next(h for h in history if h.get("version") == "2.6")
    assert v26["token"] == "XN_zExXtxu8UZWaB7pbZD1ThvmUXLNoo5lnusFX8o0E"
    assert len(v26["sections"]) >= 5

    html_out = mgr.render_history_html()
    assert "Notas de Parche v2.6" in html_out
    assert "XN_zExXtxu8UZWaB7pbZD1ThvmUXLNoo5lnusFX8o0E" in html_out
    assert "Anuncio Dedicado" in html_out or "Registro Permanente" in html_out


def test_history_dropdown_accordion_structure(tmp_patch_notes_manager):
    """Historical archive cards have dropdown headers, chevrons, collapsible bodies, and toggle handlers."""
    mgr = tmp_patch_notes_manager
    mgr.save_to_history({
        "version": "2.7",
        "title": "Notas v2.7",
        "sections": [{"icon": "✨", "title": "Sec", "items": [{"tag": "Buff", "header": "H", "desc": "D"}]}],
    })
    mgr.save_to_history({
        "version": "2.6",
        "title": "Notas v2.6",
        "sections": [{"icon": "🛠️", "title": "Sec", "items": [{"tag": "Fix", "header": "H", "desc": "D"}]}],
    })

    html = mgr.render_history_html()
    # Header dropdown attributes & markup
    assert "dropdown-chevron" in html
    assert "dropdown-arrow" in html
    assert "release-body" in html
    assert "toggleRelease" in html
    assert "btn-toggle-all" in html
    assert "toggleAllReleases" in html
    assert "aria-expanded" in html

    # All releases start collapsed by default
    assert 'class="release-card is-collapsed"' in html
    assert 'class="release-card"' not in html.replace('class="release-card is-collapsed"', "")


def test_history_deduplication_same_date_and_title(tmp_path):
    """Multiple generation runs on the same date or title merge into one card, favoring richer Gemini content."""
    data_file = str(tmp_path / "tokens.json")
    hist_file = str(tmp_path / "history.json")

    # Seed with two entries for the same date (e.g. 10/10/2026): one raw fallback, one rich Gemini
    initial_history = [
        {
            "version": "2.10.10",
            "title": "Notas de Parche (10/10/2026)",
            "created_at": 1791662749,
            "sections": [
                {
                    "icon": "🛠️",
                    "title": "Cambios Principales",
                    "items": [{"tag": "Fix", "header": "Commit header", "desc": "Commit desc"}],
                }
            ],
        },
        {
            "version": "2.7",
            "title": "Notas de Parche (10/10/2026)",
            "created_at": 1791661832,
            "sections": [
                {
                    "icon": "🧠",
                    "title": "RESPUESTAS Y COMPORTAMIENTO DEL INDIO",
                    "items": [
                        {
                            "tag": "Fix",
                            "header": "El Indio se calla cuando transcribe",
                            "desc": "Detalle creativo.",
                            "dialogues": ["Che, se calló"],
                        }
                    ],
                },
                {
                    "icon": "🌐",
                    "title": "INTERFAZ WEB",
                    "items": [{"tag": "Buff", "header": "Web UI", "desc": "Detalle web."}],
                },
            ],
        },
        {
            "version": "2.6",
            "title": "Notas de Parche v2.6",
            "created_at": 1791572630,
            "sections": [
                {
                    "icon": "🛡️",
                    "title": "Saludos Raros",
                    "items": [{"tag": "Ajuste", "header": "Cooldown", "desc": "Detalle cooldown."}],
                }
            ],
        },
    ]

    import json
    with open(hist_file, "w", encoding="utf-8") as f:
        json.dump(initial_history, f)

    mgr = PatchNotesManager(data_path=data_file, history_path=hist_file)
    hist = mgr.get_history()

    # Must be deduplicated from 3 down to 2 cards
    assert len(hist) == 2
    titles = [h["title"] for h in hist]
    assert titles.count("Notas de Parche (10/10/2026)") == 1
    assert "Notas de Parche v2.6" in titles

    # Rich Gemini entry (v2.7) must be preserved over raw fallback
    oct10_entry = next(h for h in hist if "10/10/2026" in h["title"])
    assert oct10_entry["version"] == "2.7"
    assert len(oct10_entry["sections"]) == 2


def test_history_html_does_not_contain_ultimo_lanzamiento_badge(tmp_patch_notes_manager):
    """Release cards do not display 'Último Lanzamiento' badge, newest release is simply first."""
    mgr = tmp_patch_notes_manager
    mgr.save_to_history({
        "version": "2.7",
        "title": "Notas v2.7",
        "created_at": 1791661832,
        "sections": [{"icon": "✨", "title": "Sec", "items": [{"tag": "Buff", "header": "H", "desc": "D"}]}],
    })
    mgr.save_to_history({
        "version": "2.6",
        "title": "Notas v2.6",
        "created_at": 1791572630,
        "sections": [{"icon": "🛠️", "title": "Sec", "items": [{"tag": "Fix", "header": "H", "desc": "D"}]}],
    })

    html = mgr.render_history_html()
    assert "Último Lanzamiento" not in html
    pos_27 = html.find("v2.7")
    pos_26 = html.find("v2.6")
    assert pos_27 != -1 and pos_26 != -1
    assert pos_27 < pos_26



