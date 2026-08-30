"""Behavior: Relay HTTP endpoints (/stream-watch, /stream-unwatch) control Indio's
stream spectator session."""
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"
RELAY_SECRET = "test-relay-secret"


def _load_relay_handlers():
    src_path = _USERBOT_DIR / "bot.py"
    src = src_path.read_text()
    lines = src.splitlines()

    start = next(
        i for i, line in enumerate(lines) if line.startswith("async def _relay_stream_watch(")
    )
    end = next(
        i for i, line in enumerate(lines[start + 1 :], start=start + 1)
        if line.startswith("async def _start_relay()")
    )
    block = "\n".join(lines[start:end])

    config_stub = SimpleNamespace(RELAY_SECRET=RELAY_SECRET)
    mock_spectator_mgr = SimpleNamespace(
        start_watching=AsyncMock(return_value={"watching": True}),
        stop_watching=AsyncMock(return_value=True),
        set_fast_mode=MagicMock(),
        inspect_now=AsyncMock(return_value="commentary"),
    )
    mock_client = SimpleNamespace(get_guild=lambda gid: None)

    async def mock_sync(g):
        pass

    ns: dict = {
        "config": config_stub,
        "_spectator_mgr": mock_spectator_mgr,
        "client": mock_client,
        "_sync_stream_spectator": mock_sync,
        "asyncio": __import__("asyncio"),
        "web": web,
    }
    exec(block, ns)
    return (
        ns["_relay_stream_watch"],
        ns["_relay_stream_unwatch"],
        ns.get("_relay_stream_spectator_mode"),
        config_stub,
        mock_spectator_mgr,
    )


_watch_handler, _unwatch_handler, _mode_handler, _cfg, _mock_spectator = _load_relay_handlers()


async def test_relay_stream_watch_and_unwatch():
    app = web.Application()
    app.router.add_post("/stream-watch", _watch_handler)
    app.router.add_post("/stream-unwatch", _unwatch_handler)

    tc = TestClient(TestServer(app))
    await tc.start_server()

    try:
        # Watch request
        resp = await tc.post(
            "/stream-watch",
            headers={"X-API-Secret": RELAY_SECRET},
            json={"guild_id": 1234, "streamer_name": "Miles", "streamer_id": 99},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["watching"] is True
        assert data["streamer"] == "Miles"
        _mock_spectator.start_watching.assert_called_once()

        # Unwatch request
        resp2 = await tc.post(
            "/stream-unwatch",
            headers={"X-API-Secret": RELAY_SECRET},
            json={"guild_id": 1234},
        )
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["stopped"] is True
        _mock_spectator.stop_watching.assert_called_once()
    finally:
        await tc.close()


async def test_relay_stream_spectator_mode():
    app = web.Application()
    app.router.add_post("/stream-spectator/mode", _mode_handler)

    tc = TestClient(TestServer(app))
    await tc.start_server()

    try:
        resp = await tc.post(
            "/stream-spectator/mode",
            headers={"X-API-Secret": RELAY_SECRET},
            json={"guild_id": 5555, "fast": True},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["fast"] is True
        _mock_spectator.set_fast_mode.assert_called_with(5555, fast=True)
    finally:
        await tc.close()

