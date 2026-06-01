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
