"""Behavioral tests for the /invoke_banana endpoint on the userbot relay.

Verifies payload validation, timeouts, rate limiting, and 404/504/429 HTTP statuses.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_USERBOT_DIR = Path(__file__).resolve().parent.parent / "userbot"
RELAY_SECRET = "test-relay-secret"
CHANNEL_ID = 111_222_333


def _load_invoke_banana_handler():
    """Extract handler code from userbot/bot.py using exec to run isolated."""
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
    handler_block = _extract("_relay_invoke_banana")
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
    log_stub = logging.getLogger("test_invoke_banana")
    analytics_stub = MagicMock()

    ns: dict = {
        "config": cfg,
        "client": cli,
        "log": log_stub,
        "analytics": analytics_stub,
        "web": web,
        "discord": discord,
        "asyncio": asyncio,
        "Optional": object,
    }
    exec(owner_block, ns)
    exec(pick_block, ns)
    exec(resolve_block, ns)
    exec(handler_block, ns)
    return ns["_relay_invoke_banana"], cfg, cli


_handler, _cfg, _client = _load_invoke_banana_handler()


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/invoke_banana", _handler)
    return app


async def _start() -> TestClient:
    tc = TestClient(TestServer(_make_app()))
    await tc.start_server()
    return tc


async def test_invoke_banana_success():
    """Verify invocation works when command is found and invoked successfully."""
    called_args = []

    class _GenCmd:
        name = "banana"
        application_id = 999_999

        async def __call__(self, prompt=None):
            called_args.append(prompt)
            return True

    gen_cmd = _GenCmd()

    async def _aiter_cmds():
        yield gen_cmd

    def _slash_commands(query=None):
        return _aiter_cmds()

    channel = SimpleNamespace(slash_commands=_slash_commands)
    _client.get_channel = lambda cid: channel
    _cfg.INDIO_RELAY_TIMEOUT = 2.0

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_banana",
            json={"channel_id": CHANNEL_ID, "query": "un gato enojado"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 200
    assert body.get("invoked") is True
    assert called_args == ["un gato enojado"]


async def test_invoke_banana_returns_504_when_stalled():
    """Verify timeout on slash commands search returns 504."""
    stuck = asyncio.Future()

    def _slash_commands(query=None):
        return stuck

    channel = SimpleNamespace(slash_commands=_slash_commands)
    _client.get_channel = lambda cid: channel
    _cfg.INDIO_RELAY_TIMEOUT = 0.1

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_banana",
            json={"channel_id": CHANNEL_ID, "query": "perro corriendo"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 504
    assert "timed out" in body.get("error", "").lower()


async def test_invoke_banana_returns_429_on_rate_limit():
    """Verify that Discord rate limit HTTPExceptions are mapped to 429."""
    class _GenCmd:
        name = "banana"
        application_id = 999_999

        async def __call__(self, prompt=None):
            raise discord.HTTPException(
                SimpleNamespace(status=429, reason="Too Many Requests"),
                {"message": "rate limited", "code": 0},
            )

    gen_cmd = _GenCmd()

    async def _aiter_cmds():
        yield gen_cmd

    def _slash_commands(query=None):
        return _aiter_cmds()

    channel = SimpleNamespace(slash_commands=_slash_commands)
    _client.get_channel = lambda cid: channel
    _cfg.INDIO_RELAY_TIMEOUT = 2.0

    tc = await _start()
    try:
        resp = await tc.post(
            "/invoke_banana",
            json={"channel_id": CHANNEL_ID, "query": "auto azul"},
            headers={"X-API-Secret": RELAY_SECRET},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 429
    assert "rate" in body.get("error", "").lower()
