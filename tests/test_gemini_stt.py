"""Behavioral and unit tests for Gemini STT integration in the userbot."""

import asyncio
import io
import sys
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import geminiKeys
import groqKeys

if "discord.ext.voice_recv" not in sys.modules:
    class MockVoiceRecv(MagicMock):
        class AudioSink: pass
    sys.modules["discord.ext.voice_recv"] = MockVoiceRecv()

import discord
if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

for _mod in ("vosk", "davey", "dave", "davey_compat"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

if "numpy" not in sys.modules:
    mock_np = MagicMock()
    mock_np.bool_ = bool
    sys.modules["numpy"] = mock_np

from userbot.bot import _run_gemini_stt, _transcribe_pcm, _stt_confirms_indio


def _make_dummy_pcm(duration_sec: float = 1.0) -> bytes:
    """Create dummy s16le 16kHz mono audio bytes."""
    samples = int(16000 * duration_sec)
    return b"\x00\x00" * samples


@pytest.fixture(autouse=True)
def reset_keys_cooldown():
    geminiKeys._key_cooldowns.clear()
    groqKeys._key_cooldowns.clear()
    yield
    geminiKeys._key_cooldowns.clear()
    groqKeys._key_cooldowns.clear()


class MockAsyncContext:
    def __init__(self, resp):
        self.resp = resp
    async def __aenter__(self):
        return self.resp
    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_run_gemini_stt_success(monkeypatch):
    monkeypatch.setattr(geminiKeys, "active_keys", lambda: ["AIzaTestKey123"])

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Paciente de cáncer."}]
                }
            }
        ]
    })

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=MockAsyncContext(mock_resp))

    with patch("userbot.bot._get_http", AsyncMock(return_value=mock_session)):
        res = await _run_gemini_stt(_make_dummy_pcm(1.0))
        assert res == "Paciente de cáncer."


@pytest.mark.asyncio
async def test_run_gemini_stt_key_rotation_on_429(monkeypatch):
    monkeypatch.setattr(geminiKeys, "active_keys", lambda: ["AIzaKey111", "AIzaKey222"])

    resp_429 = MagicMock()
    resp_429.status = 429
    resp_429.text = AsyncMock(return_value="Resource has been exhausted")

    resp_200 = MagicMock()
    resp_200.status = 200
    resp_200.json = AsyncMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "Che indio poné música"}]}}]
    })

    call_count = 0
    def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = resp_429 if call_count == 1 else resp_200
        return MockAsyncContext(resp)

    mock_session = MagicMock()
    mock_session.post = fake_post

    with patch("userbot.bot._get_http", AsyncMock(return_value=mock_session)):
        res = await _run_gemini_stt(_make_dummy_pcm(1.0))
        assert res == "Che indio poné música"
        assert call_count == 2
        assert geminiKeys._key_cooldowns.get("AIzaKey111", 0) > 0


@pytest.mark.asyncio
async def test_transcribe_pcm_gemini_fallback_to_groq(monkeypatch):
    monkeypatch.setattr(geminiKeys, "active_keys", lambda: ["AIzaKeyFail"])
    monkeypatch.setattr(groqKeys, "active_keys", lambda: ["gsk_valid_fallback"])

    with patch("userbot.bot._run_gemini_stt", AsyncMock(return_value="")), \
         patch("userbot.bot._run_groq_stt", AsyncMock(return_value="Hola desde Groq")) as mock_groq:
        res = await _transcribe_pcm(_make_dummy_pcm(1.0))
        assert res == "Hola desde Groq"
        mock_groq.assert_called_once()


def test_stt_confirms_indio():
    # Valid triggers
    assert _stt_confirms_indio("che indio qué onda") is True
    assert _stt_confirms_indio("Indio, ponete un tema") is True
    assert _stt_confirms_indio("che india vení") is True
    assert _stt_confirms_indio("che sendio clipeá esto") is True

    # Rejection of false positive (Bug from Issue #80)
    assert _stt_confirms_indio("paciente de cáncer") is False
    assert _stt_confirms_indio("¿Pasin de ganasen?") is False

    # Rejection of 3rd person references
    assert _stt_confirms_indio("viste lo que dijo el indio solari") is False
    assert _stt_confirms_indio("hablamos del indio recién") is False

    # Empty
    assert _stt_confirms_indio("") is False
    assert _stt_confirms_indio(None) is False
