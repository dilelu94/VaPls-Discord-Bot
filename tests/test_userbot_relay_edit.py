"""Behavioral tests for POST /edit on the userbot relay HTTP server.

Observable promise: when the main bot asks the userbot to edit a previously
posted message, the right Discord message receives the new content and the
caller gets a 200 with the message id.  Auth and not-found paths return the
correct status codes so the main bot can react appropriately.

Boundary mocked: the Discord ``client`` (get_channel / fetch_channel /
fetch_message / edit) — a real process edge. We do NOT mock aiohttp itself;
we run the handler through a real aiohttp TestClient so the routing, JSON
serialisation and HTTP headers are exercised for real.
"""

from __future__ import annotations

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
MESSAGE_ID = 999_888_777


# ---------------------------------------------------------------------------
# Extract _relay_edit from source with injected stubs
# ---------------------------------------------------------------------------


def _load_relay_edit():
    """Extract _relay_edit from userbot/bot.py without running discord setup.

    We read the source, locate the function definition, and exec just that
    block into a clean namespace that has stubs for the three globals the
    handler uses: ``config``, ``client``, and ``log``.  The stubs are returned
    so individual tests can mutate them via SimpleNamespace attribute assignment.
    """
    src_path = _USERBOT_DIR / "bot.py"
    src = src_path.read_text()
    lines = src.splitlines()

    # Find the start of _relay_edit
    start = next(
        i for i, line in enumerate(lines) if line.startswith("async def _relay_edit(")
    )
    # Find the next top-level definition after _relay_edit
    end = next(
        i
        for i, line in enumerate(lines[start + 1 :], start=start + 1)
        if line.startswith(("async def ", "def ", "class "))
    )
    block = "\n".join(lines[start:end])

    config_stub = SimpleNamespace(RELAY_SECRET=RELAY_SECRET)
    client_stub = SimpleNamespace(
        is_ready=lambda: True,
        get_channel=lambda cid: None,
        fetch_channel=AsyncMock(side_effect=Exception("not found")),
    )
    log_stub = logging.getLogger("test_relay_edit")

    analytics_stub = SimpleNamespace(capture_exception=lambda e, **kw: None)

    ns: dict = {
        "config": config_stub,
        "client": client_stub,
        "log": log_stub,
        "web": web,
        "discord": discord,
        "analytics": analytics_stub,
    }
    exec(block, ns)
    return ns["_relay_edit"], config_stub, client_stub


_handler, _cfg, _client_stub = _load_relay_edit()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/edit", _handler)
    return app


async def _start(app: web.Application) -> TestClient:
    tc = TestClient(TestServer(app))
    await tc.start_server()
    return tc


def _good_headers() -> dict:
    return {"X-API-Secret": RELAY_SECRET}


def _make_channel(message_id: int = MESSAGE_ID, content: str = "original"):
    """Return a fake channel whose fetch_message returns a fake message."""
    msg = SimpleNamespace(
        id=message_id,
        content=content,
        edit=AsyncMock(),
    )
    channel = SimpleNamespace(
        id=CHANNEL_ID,
        fetch_message=AsyncMock(return_value=msg),
    )
    return channel, msg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_edit_updates_message_content_and_returns_200():
    """The target message's edit() is called with the new content, and the
    response is 200 with ok=true and the message_id."""
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: channel
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={
                "channel_id": CHANNEL_ID,
                "message_id": MESSAGE_ID,
                "content": "updated text",
            },
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 200
    assert body["ok"] is True
    assert body["message_id"] == MESSAGE_ID
    # The new content actually reached the Discord message boundary.
    msg.edit.assert_called_once()
    _, kwargs = msg.edit.call_args
    assert kwargs.get("content") == "updated text"


async def test_missing_secret_returns_401():
    """A request without the correct X-API-Secret is rejected before any
    Discord call is made."""
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: channel
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
            headers={"X-API-Secret": "wrong-secret"},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 401
    assert "unauthorized" in body.get("error", "")
    msg.edit.assert_not_called()


async def test_missing_secret_header_entirely_returns_401():
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: channel

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 401
    msg.edit.assert_not_called()


async def test_unknown_channel_returns_404():
    """When get_channel returns None and fetch_channel raises, the caller gets
    a 404 with a channel-not-found error."""
    _client_stub.get_channel = lambda cid: None
    _client_stub.fetch_channel = AsyncMock(side_effect=Exception("no such channel"))
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 404
    assert "channel not found" in body.get("error", "")


async def test_unknown_message_returns_404():
    """When fetch_message raises, the caller gets a 404 with a message-not-found
    error so it knows whether the channel or the message is the problem."""
    bad_channel = SimpleNamespace(
        id=CHANNEL_ID,
        fetch_message=AsyncMock(side_effect=Exception("no such msg")),
    )
    _client_stub.get_channel = lambda cid: bad_channel
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 404
    assert "message not found" in body.get("error", "")


async def test_missing_required_field_returns_400():
    """A body that omits message_id cannot be parsed correctly; the handler
    must reject it with 400 before touching Discord."""
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: channel
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "content": "no message_id here"},
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 400
    assert "invalid body" in body.get("error", "")
    msg.edit.assert_not_called()


async def test_relay_disabled_returns_503_when_no_secret_configured():
    """When RELAY_SECRET is falsy the endpoint immediately returns 503 so the
    main bot knows relay is not available, without leaking internals."""
    original = _cfg.RELAY_SECRET
    _cfg.RELAY_SECRET = ""
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: channel

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()
        _cfg.RELAY_SECRET = original

    assert resp.status == 503
    assert "relay disabled" in body.get("error", "")
    msg.edit.assert_not_called()


async def test_userbot_not_ready_returns_503():
    """If the Discord client is still connecting, the endpoint must return
    503 so the caller can retry rather than getting a confusing error."""
    _client_stub.is_ready = lambda: False
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: channel

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()
        _client_stub.is_ready = lambda: True  # restore

    assert resp.status == 503
    assert "userbot not ready" in body.get("error", "")
    msg.edit.assert_not_called()


async def test_edit_failure_returns_500():
    """If msg.edit() raises (e.g. permissions error), the response is 500 with
    the exception string so the main bot can log and handle it."""
    channel, msg = _make_channel()
    msg.edit = AsyncMock(side_effect=Exception("missing permissions"))
    _client_stub.get_channel = lambda cid: channel
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "new"},
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 500
    assert "missing permissions" in body.get("error", "")


async def test_fetch_channel_fallback_used_when_get_channel_misses():
    """If the channel is not in the cache (get_channel returns None),
    fetch_channel is used as a fallback and the edit still succeeds."""
    channel, msg = _make_channel()
    _client_stub.get_channel = lambda cid: None
    _client_stub.fetch_channel = AsyncMock(return_value=channel)
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={
                "channel_id": CHANNEL_ID,
                "message_id": MESSAGE_ID,
                "content": "via fetch",
            },
            headers=_good_headers(),
        )
        body = await resp.json()
    finally:
        await tc.close()

    assert resp.status == 200
    assert body["ok"] is True
    msg.edit.assert_called_once()


async def test_non_messageable_channel_returns_400():
    """A resolved channel that isn't messageable (e.g. a category, which has
    no fetch_message) must return a clear 400 instead of a misleading 404."""
    category_like = SimpleNamespace(id=CHANNEL_ID)  # no fetch_message attr
    _client_stub.get_channel = lambda cid: category_like
    _client_stub.fetch_channel = AsyncMock(side_effect=Exception("not found"))
    _client_stub.is_ready = lambda: True

    tc = await _start(_make_app())
    try:
        resp = await tc.post(
            "/edit",
            json={"channel_id": CHANNEL_ID, "message_id": MESSAGE_ID, "content": "x"},
            headers=_good_headers(),
        )
    finally:
        await tc.close()

    assert resp.status == 400
