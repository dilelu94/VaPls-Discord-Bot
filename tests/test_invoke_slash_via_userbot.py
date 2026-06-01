"""Behavior: _invoke_slash_via_userbot is the indio's bridge that asks the
userbot (a real Discord account) to fire a slash command. It must respect
the configured timeout — not a hardcoded one — and it must build the relay
URL correctly regardless of trailing slashes or path segments in
INDIO_RELAY_URL.

Boundary mocked: the userbot's HTTP server is replaced by a real
``aiohttp.TestServer`` exposing the same routes. No mocks of our own code
or of aiohttp internals — the request is sent and parsed end-to-end.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


async def test_timeout_respects_indio_relay_timeout(monkeypatch):
    """When INDIO_RELAY_TIMEOUT is low and the userbot is slow, the call
    must fail with a timeout error in roughly that window — not the legacy
    hardcoded 10s.
    """
    import geminiCommand

    async def _slow_handler(request):
        await asyncio.sleep(1.0)
        return web.json_response({"invoked": True})

    app = web.Application()
    app.router.add_post("/invoke_play", _slow_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}/say"
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_URL", base,
                            raising=False)
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_SECRET",
                            "test-secret", raising=False)
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_TIMEOUT", 0.1,
                            raising=False)

        start = asyncio.get_event_loop().time()
        ok, msg = await geminiCommand._invoke_slash_via_userbot(
            "invoke_play", channel_id=42, query="despacito"
        )
        elapsed = asyncio.get_event_loop().time() - start

        assert ok is False
        # Failed fast — the legacy 10s hardcode would have blocked here.
        assert elapsed < 0.7, f"timeout did not fire in time (took {elapsed:.2f}s)"
    finally:
        await server.close()


async def test_succeeds_when_userbot_responds_within_timeout(monkeypatch):
    """Counterpart sanity check: when the userbot responds quickly the call
    returns (True, query) so the timeout fix doesn't create false negatives.
    """
    import geminiCommand

    received: list[dict] = []

    async def _fast_handler(request):
        received.append(await request.json())
        return web.json_response({"invoked": True, "query": "despacito"})

    app = web.Application()
    app.router.add_post("/invoke_play", _fast_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}/say"
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_URL", base,
                            raising=False)
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_SECRET",
                            "test-secret", raising=False)
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_TIMEOUT", 2.0,
                            raising=False)

        ok, msg = await geminiCommand._invoke_slash_via_userbot(
            "invoke_play", channel_id=42, query="despacito"
        )

        assert ok is True
        assert msg == "despacito"
        assert received and received[0]["query"] == "despacito"
    finally:
        await server.close()


@pytest.mark.parametrize("path_suffix", [
    "/say",         # canonical default — INDIO_RELAY_URL points to /say
    "/say/",        # trailing slash — common copy-paste mistake
    "/",            # base URL ends at the host root
    "/api/say",     # multi-segment path (reverse proxy mounted under /api)
])
async def test_url_resolution_is_robust_to_indio_relay_url_shape(
        monkeypatch, path_suffix):
    """The fixed URL builder must always hit /invoke_play at the host root,
    regardless of how INDIO_RELAY_URL is shaped (trailing slash, sub-paths,
    etc.). Without this, deployments that mount the userbot relay behind a
    proxy or that accidentally include a trailing slash get 404s.
    """
    import geminiCommand

    app = web.Application()

    async def _ok_handler(request):
        return web.json_response({"invoked": True})

    # Only route registered at the canonical /invoke_play path. A wrong
    # URL build would 404 here.
    app.router.add_post("/invoke_play", _ok_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}{path_suffix}"
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_URL", base,
                            raising=False)
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_SECRET",
                            "test-secret", raising=False)
        monkeypatch.setattr(geminiCommand.config, "INDIO_RELAY_TIMEOUT", 2.0,
                            raising=False)

        ok, msg = await geminiCommand._invoke_slash_via_userbot(
            "invoke_play", channel_id=42, query="x"
        )

        assert ok is True, (
            f"URL resolution failed for INDIO_RELAY_URL={base!r}: {msg}"
        )
    finally:
        await server.close()
