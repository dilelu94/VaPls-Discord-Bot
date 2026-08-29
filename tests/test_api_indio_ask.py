"""Behavioral tests for HTTP API endpoints POST /indio/ask and GET /audio/{filename}."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import geminiCommand
import tts
from apiServer import makeApp

API_SECRET = "test-secret"
HEADERS = {"X-API-Secret": API_SECRET}


@pytest.fixture(autouse=True)
def _api_secret(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET", API_SECRET, raising=False)


async def _client_for_bot(bot) -> TestClient:
    app = makeApp(bot)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_indio_ask_without_auth_fails():
    """Requests to POST /indio/ask without X-API-Secret header are rejected with 401."""
    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.post("/indio/ask", json={"prompt": "hola"})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_indio_ask_empty_prompt_returns_400():
    """Posting an empty prompt returns a 400 error."""
    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.post(
            "/indio/ask",
            json={"guild_id": 123, "prompt": "   ", "speaker": "Leonel"},
            headers=HEADERS,
        )
        assert resp.status == 400
        data = await resp.json()
        assert data.get("error") == "empty prompt"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_indio_ask_returns_text_without_tts(monkeypatch):
    """POST /indio/ask with generate_tts=false generates Indio reply text and null audio_url."""
    async def _fake_generate_indio(guild_id, prompt, speaker, bot=None):
        return f"Qué hacés {speaker}, todo piola?"

    monkeypatch.setattr(
        geminiCommand, "generate_indio_telegram_response", _fake_generate_indio
    )
    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.post(
            "/indio/ask",
            json={
                "guild_id": 451580655650996236,
                "prompt": "hola",
                "speaker": "Leonel",
                "generate_tts": False,
            },
            headers=HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["text"] == "Indio: Qué hacés Leonel, todo piola?"
        assert data["audio_url"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_indio_ask_returns_text_and_audio_url_with_tts(monkeypatch):
    """POST /indio/ask with generate_tts=true synthesizes TTS audio and returns relative audio_url."""
    async def _fake_generate_indio(guild_id, prompt, speaker, bot=None):
        return "Qué hacés Leonel, todo piola?"

    def _fake_generate_tts(text, output_dir="/tmp/tts_audios"):
        return "indio_resp_123.ogg"

    monkeypatch.setattr(
        geminiCommand, "generate_indio_telegram_response", _fake_generate_indio
    )
    monkeypatch.setattr(tts, "generate_indio_tts", _fake_generate_tts)

    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.post(
            "/indio/ask",
            json={
                "guild_id": 451580655650996236,
                "prompt": "hola",
                "speaker": "Leonel",
                "chat_id": -1001356429500,
                "generate_tts": True,
            },
            headers=HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["text"] == "Indio: Qué hacés Leonel, todo piola?"
        assert data["audio_url"] == "/audio/indio_resp_123.ogg"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_audio_serves_existing_file(tmp_path, monkeypatch):
    """GET /audio/{filename} serves the requested audio file without needing auth header."""
    audio_dir = tmp_path / "tts_audios"
    audio_dir.mkdir()
    audio_file = audio_dir / "indio_resp_test.ogg"
    audio_file.write_bytes(b"OggS_fake_audio_content")

    # Override /tmp/tts_audios target in getAudio by monkeypatching os.path.join
    original_join = os.path.join

    def _fake_join(*args):
        if args and args[0] == "/tmp/tts_audios":
            return original_join(str(audio_dir), *args[1:])
        return original_join(*args)

    monkeypatch.setattr(os.path, "join", _fake_join)

    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.get("/audio/indio_resp_test.ogg")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/ogg"
        content = await resp.read()
        assert content == b"OggS_fake_audio_content"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_audio_returns_404_for_missing_file():
    """GET /audio/{filename} returns 404 JSON error when file is not found."""
    bot = AsyncMock(name="DiscordBot")
    client = await _client_for_bot(bot)
    try:
        resp = await client.get("/audio/non_existent_file.ogg")
        assert resp.status == 404
        data = await resp.json()
        assert data.get("error") == "Audio not found"
    finally:
        await client.close()
