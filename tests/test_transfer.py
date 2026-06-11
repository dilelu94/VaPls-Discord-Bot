"""Behavioral tests for /transferir: session lifecycle, upload expiry, embed.

Covers:
- TransferSession.completed_at default and set on complete
- uploadStatus changes from completed → expired after SESSION_TTL
- Download endpoint remains available after upload page expires
- extract_role_check for @Main Characters gating
- uploadComplete endpoint posts Discord embed via bot
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import transferCommand
from apiServer import makeApp


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_SECRET", "test-secret", raising=False)
    monkeypatch.setattr(
        config, "TRANSFER_DIR", str(tmp_path / "transfers"), raising=False
    )
    monkeypatch.setattr(
        config, "TRANSFER_HISTORY_PATH", str(tmp_path / "_history.jsonl"), raising=False
    )
    monkeypatch.setattr(config, "TRANSFER_SESSION_TTL", 300, raising=False)
    monkeypatch.setattr(config, "TRANSFER_EXPIRY_HOURS", 24, raising=False)
    monkeypatch.setattr(config, "TRANSFER_DEFAULT_LIMIT", 10 * 1024**3, raising=False)
    monkeypatch.setattr(config, "TRANSFER_MAX_SIZE", 15 * 1024**3, raising=False)
    monkeypatch.setattr(config, "TRANSFER_CHUNK_SIZE", 10 * 1024**2, raising=False)
    monkeypatch.setattr(config, "TRANSFER_DISK_RESERVE", 0, raising=False)
    monkeypatch.setattr(config, "TRANSFER_BASE_URL", "http://test.local", raising=False)
    monkeypatch.setattr(
        config, "TRANSFER_REQUIRED_ROLE", "Main Characters", raising=False
    )


@pytest.fixture(autouse=True)
def _fresh_manager(monkeypatch, tmp_path):
    mgr = transferCommand.TransferManager()
    monkeypatch.setattr(transferCommand, "manager", mgr, raising=False)
    return mgr


async def _client():
    app = makeApp(MagicMock())
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def _complete_upload(mgr, token, data=b"chunk data"):
    mgr.init_upload(token, "test.txt", len(data))
    mgr.add_chunk(token, 0, data)
    mgr.complete_upload(token)
    return mgr.get(token)


# --- TransferSession.completed_at -------------------------------------------


def test_completed_at_default_is_none():
    sess = transferCommand.TransferSession(
        token="abc", author_id=1, author_name="t", channel_id=1, guild_id=1
    )
    assert sess.completed_at is None


def test_completed_at_set_on_complete(_fresh_manager):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    _complete_upload(mgr, sess.token)
    assert sess.completed_at is not None
    assert isinstance(sess.completed_at, float)
    assert sess.completed_at > 0


def test_completed_at_stays_none_on_incomplete(_fresh_manager):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    assert sess.completed_at is None


# --- uploadStatus expiry after completion -----------------------------------


async def test_status_completed_not_expired_right_after_upload(
    _fresh_manager, tmp_path
):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    _complete_upload(mgr, sess.token)

    client = await _client()
    try:
        resp = await client.get(f"/upload/{sess.token}/status")
        body = await resp.json()
    finally:
        await client.close()

    assert body["valid"] is True
    assert body["completed"] is True
    assert body["expired"] is False
    assert body["ttl_remaining"] > 0


async def test_status_upload_expired_after_session_ttl_from_completion(
    monkeypatch, _fresh_manager, tmp_path
):
    monkeypatch.setattr(config, "TRANSFER_SESSION_TTL", 5, raising=False)
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    sess.completed = True
    sess.ready = True
    sess.completed_at = time.time() - 10

    client = await _client()
    try:
        resp = await client.get(f"/upload/{sess.token}/status")
        body = await resp.json()
    finally:
        await client.close()

    assert body["valid"] is True
    assert body["completed"] is True
    assert body["expired"] is True
    assert body["ttl_remaining"] == 0


async def test_download_still_works_after_upload_expired(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    sess.completed = True
    sess.ready = True
    sess.completed_at = time.time() - 600

    token_dir = tmp_path / "transfers" / sess.token
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "test.txt").write_text("hello world")

    client = await _client()
    try:
        status_resp = await client.get(f"/upload/{sess.token}/status")
        status_body = await status_resp.json()
        assert status_body["expired"] is True

        dl_resp = await client.get(f"/dl/{sess.token}/test.txt")
        dl_body = await dl_resp.text()
    finally:
        await client.close()

    assert dl_resp.status == 200
    assert dl_body == "hello world"


async def test_status_expired_for_inactive_uncompleted(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    sess.last_activity = time.time() - 600

    client = await _client()
    try:
        resp = await client.get(f"/upload/{sess.token}/status")
        body = await resp.json()
    finally:
        await client.close()

    assert body["valid"] is True
    assert body["completed"] is False
    assert body["expired"] is True


# --- required role check (extracted from bot.py) -----------------------------


def _has_transfer_role(author_roles, role_name):
    import discord

    return discord.utils.get(author_roles, name=role_name) is not None


def test_role_gate_passes_with_main_characters():
    role = MagicMock()
    role.name = "Main Characters"
    assert _has_transfer_role([role], "Main Characters") is True


def test_role_gate_fails_without_role():
    role = MagicMock()
    role.name = "Everyone"
    assert _has_transfer_role([role], "Main Characters") is False


def test_role_gate_fails_with_empty_roles():
    assert _has_transfer_role([], "Main Characters") is False


# --- history entry includes token -------------------------------------------


def test_history_entry_has_token(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    _complete_upload(mgr, sess.token)

    history = mgr.get_history(limit=10)
    assert len(history) >= 1
    entry = history[0]
    assert entry["token"] == sess.token
    assert entry["filename"] == "test.txt"
    assert isinstance(entry["uploaded_at"], int)


# --- endpoints reject when upload expired ------------------------------------


async def test_delete_works_even_when_upload_expired(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    sess.completed = True
    sess.ready = True
    sess.completed_at = None  # legacy → upload expired

    client = await _client()
    try:
        resp = await client.delete(f"/upload/{sess.token}")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["ok"] is True
    assert sess.expired is True


async def test_files_rejected_when_upload_expired(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    sess.completed = True
    sess.ready = True
    sess.completed_at = None

    client = await _client()
    try:
        resp = await client.get(f"/upload/{sess.token}/files")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 403
    assert "expirada" in body.get("error", "")


# --- completed file appears in active list ----------------------------------


def test_completed_file_appears_in_active_list(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    _complete_upload(mgr, sess.token)

    history = mgr.get_history(limit=10)
    assert len(history) >= 1
    entry = history[0]
    assert entry["token"] == sess.token
    assert entry["filename"] == "test.txt"
    assert isinstance(entry["uploaded_at"], int)


# --- uploadComplete posts Discord embed --------------------------------------


async def test_upload_complete_posts_embed_to_correct_channel(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    mgr.init_upload(sess.token, "test.txt", 5)
    mgr.add_chunk(sess.token, 0, b"hello")

    bot = MagicMock()
    ch = AsyncMock()
    bot.get_channel.return_value = ch

    app = makeApp(bot)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post(f"/upload/{sess.token}/complete")
        body = await resp.json()
    finally:
        await c.close()

    assert resp.status == 200
    assert body["ok"] is True
    bot.get_channel.assert_called_once_with(42)
    ch.send.assert_awaited_once()


async def test_upload_complete_embed_contains_download_button(_fresh_manager, tmp_path):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    mgr.init_upload(sess.token, "test.txt", 5)
    mgr.add_chunk(sess.token, 0, b"hello")

    bot = MagicMock()
    ch = AsyncMock()
    bot.get_channel.return_value = ch

    app = makeApp(bot)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        await c.post(f"/upload/{sess.token}/complete")
    finally:
        await c.close()

    args, kwargs = ch.send.await_args
    assert "view" in kwargs
    view = kwargs["view"]
    assert len(view.children) == 1
    btn = view.children[0]
    assert "Descargar" in btn.label
    assert btn.url is not None
    assert sess.token in btn.url


async def test_upload_complete_returns_ok_even_when_channel_gone(
    _fresh_manager, tmp_path
):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    mgr.init_upload(sess.token, "test.txt", 5)
    mgr.add_chunk(sess.token, 0, b"hello")

    bot = MagicMock()
    bot.get_channel.return_value = None

    app = makeApp(bot)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post(f"/upload/{sess.token}/complete")
        body = await resp.json()
    finally:
        await c.close()

    assert resp.status == 200
    assert body["ok"] is True


async def test_status_returns_file_exists_and_filename_when_expired(
    _fresh_manager, tmp_path
):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    _complete_upload(mgr, sess.token)
    sess.completed_at = time.time() - 600  # expired

    token_dir = tmp_path / "transfers" / sess.token
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "test.txt").write_text("hello")

    client = await _client()
    try:
        resp = await client.get(f"/upload/{sess.token}/status")
        body = await resp.json()
    finally:
        await client.close()

    assert "file_exists" in body
    assert body["file_exists"] is True
    assert body["filename"] == "test.txt"
    assert body["expired"] is True


async def test_upload_complete_returns_ok_even_when_discord_send_fails(
    _fresh_manager, tmp_path
):
    mgr = _fresh_manager
    sess = mgr.create_session(1, "tester", 42, 100)
    mgr.init_upload(sess.token, "test.txt", 5)
    mgr.add_chunk(sess.token, 0, b"hello")

    bot = MagicMock()
    ch = AsyncMock()
    ch.send.side_effect = Exception("discord outage")
    bot.get_channel.return_value = ch

    app = makeApp(bot)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        resp = await c.post(f"/upload/{sess.token}/complete")
        body = await resp.json()
    finally:
        await c.close()

    assert resp.status == 200
    assert body["ok"] is True
