"""Behavior: POST /invoke_play on the userbot relay must not hang when
``channel.slash_commands()`` stalls. discord.py-self has no built-in
cancellation hook there, and the main bot's 10-second client timeout
returns to the indio dispatcher long before the userbot stops waiting
on Discord. The fix gates the call with ``asyncio.wait_for`` using
``config.INDIO_RELAY_TIMEOUT`` so a stuck Discord cache lookup surfaces
as a clean 504 instead of an open handler.

Boundary mocked: the discord ``client`` (get_channel / slash_commands)
and ``config``. We DO run the actual handler over a real aiohttp test
server so request parsing and timeout-to-504 translation are exercised.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"
RELAY_SECRET = "test-relay-secret"
CHANNEL_ID = 111_222_333


def _load_invoke_play_handler():
    """Extract _resolve_slash_commands + _relay_invoke_play from the userbot
    source and exec them into a clean namespace with stubs. Mirrors the
    pattern in test_userbot_relay_edit.py — we avoid importing the whole
    userbot module (which would try to connect to Discord).
    """
    src = (_USERBOT_DIR / "bot.py").read_text().splitlines()

    def _extract(name: str):
        start = next(
            i for i, line in enumerate(src)
            if line.startswith(f"async def {name}(") or line.startswith(f"def {name}(")
        )
        end = next(
            i for i, line in enumerate(src[start + 1:], start=start + 1)
            if line.startswith(("async def ", "def ", "class "))
        )
        return "\n".join(src[start:end])

    resolve_block = _extract("_resolve_slash_commands")
    handler_block = _extract("_relay_invoke_play")
    pick_block = _extract("_pick_vapls_command")
    owner_block = _extract("_command_owner_id")

    cfg = SimpleNamespace(
        RELAY_SECRET=RELAY_SECRET,
        INDIO_RELAY_TIMEOUT=0.2,
        VAPLS_BOT_ID=999_999,
    )
    cli = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda cid: None,
        fetch_channel=AsyncMock(side_effect=Exception("not found")),
    )
    log_stub = logging.getLogger("test_invoke_play")

    ns: dict = {
        "config": cfg,
        "client": cli,
        "log": log_stub,
        "web": web,
        "discord": discord,
        "asyncio": asyncio,
        "Optional": object,  # used only in type hints inside _command_owner_id
    }
    # Order matters: helpers first, then handler.
    exec(owner_block, ns)
    exec(pick_block, ns)
    exec(resolve_block, ns)
    exec(handler_block, ns)
    return ns["_relay_invoke_play"], cfg, cli


_handler, _cfg, _client = _load_invoke_play_handler()


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/invoke_play", _handler)
    return app


async def _start() -> TestClient:
    tc = TestClient(TestServer(_make_app()))
    await tc.start_server()
    return tc


async def test_invoke_play_returns_504_when_slash_commands_stalls():
    """The observable promise: a stuck Discord cache lookup surfaces as a
    timely 504 instead of blocking the relay forever. Keeps the indio's
    fallback path responsive when Discord is misbehaving.
    """
    # Build a channel whose slash_commands() hangs forever.
    stuck = asyncio.Future()  # never resolves

    def _slash_commands(query=None):
        return stuck

    channel = SimpleNamespace(slash_commands=_slash_commands)
    _client.get_channel = lambda cid: channel
    _cfg.INDIO_RELAY_TIMEOUT = 0.2  # short-circuit fast

    tc = await _start()
    try:
        start = asyncio.get_event_loop().time()
        resp = await tc.post(
            "/invoke_play",
            json={"channel_id": CHANNEL_ID, "query": "despacito"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        elapsed = asyncio.get_event_loop().time() - start
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 504, f"expected 504 on timeout, got {resp.status}"
    assert "timed out" in body.get("error", "").lower()
    # Returned within a small multiple of the configured timeout, not 10s.
    assert elapsed < 1.0, f"timeout did not fire in time (took {elapsed:.2f}s)"


async def test_invoke_play_returns_404_when_no_vapls_command_found():
    """Sanity: with a working channel that exposes other bots' /play but
    none owned by VaPls, the handler still returns 404 (no regression).
    The timeout path doesn't shadow this case.
    """
    async def _async_iter():
        # Simulates discord.py-self's async iterator. Empty = no commands
        # match the VaPls bot id filter.
        if False:
            yield None

    def _slash_commands(query=None):
        return _async_iter()

    channel = SimpleNamespace(slash_commands=_slash_commands)
    _client.get_channel = lambda cid: channel
    _cfg.INDIO_RELAY_TIMEOUT = 2.0

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_play",
            json={"channel_id": CHANNEL_ID, "query": "x"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
    finally:
        await tc.close()

    assert resp.status == 404
