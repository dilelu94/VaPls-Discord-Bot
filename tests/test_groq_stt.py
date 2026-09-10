import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import aiohttp
import discord

if "discord.ext.voice_recv" not in sys.modules:
    sys.modules["discord.ext.voice_recv"] = MagicMock()
if "discord.voice_state" not in sys.modules:
    mock_vs = types.ModuleType("discord.voice_state")
    mock_vs.VoiceConnectionState = MagicMock()
    sys.modules["discord.voice_state"] = mock_vs
    discord.voice_state = mock_vs

from userbot import config
from userbot import bot as userbot_bot


@pytest.mark.asyncio
async def test_run_groq_stt_success(monkeypatch):
    """Verify _run_groq_stt sends wav buffer and headers to Groq API and returns transcript."""
    monkeypatch.setattr(userbot_bot.config, "GROQ_API_KEY", "gsk_test_key_123")
    monkeypatch.setattr(userbot_bot.config, "GROQ_MODEL", "whisper-large-v3-turbo")

    fake_response = AsyncMock()
    fake_response.status = 200
    fake_response.json.return_value = {"text": "che indio ponete un tema"}

    fake_post_cm = AsyncMock()
    fake_post_cm.__aenter__.return_value = fake_response
    fake_post_cm.__aexit__.return_value = None

    fake_session = MagicMock()
    fake_session.post.return_value = fake_post_cm

    monkeypatch.setattr(userbot_bot, "_get_http", AsyncMock(return_value=fake_session))

    pcm_dummy = b"\x00\x00" * 16000  # 1 second of 16kHz mono silence
    text = await userbot_bot._run_groq_stt(pcm_dummy)

    assert text == "che indio ponete un tema"
    fake_session.post.assert_called_once()
    call_args, call_kwargs = fake_session.post.call_args
    assert call_args[0] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert call_kwargs["headers"]["Authorization"] == "Bearer gsk_test_key_123"


@pytest.mark.asyncio
async def test_transcribe_pcm_routing_to_groq(monkeypatch):
    """Verify _transcribe_pcm delegates to _run_groq_stt when provider is groq."""
    monkeypatch.setattr(userbot_bot.config, "STT_PROVIDER", "groq")
    monkeypatch.setattr(userbot_bot.config, "GROQ_API_KEY", "gsk_test_key_123")

    with patch.object(userbot_bot, "_run_groq_stt", AsyncMock(return_value="che indio hola")) as mock_groq:
        pcm_dummy = b"\x00\x00" * 8000
        result = await userbot_bot._transcribe_pcm(pcm_dummy)
        assert result == "che indio hola"
        mock_groq.assert_called_once_with(pcm_dummy)


def test_whisper_confirms_indio_strict_verification():
    """Verify strict wake-word confirmation accepts indio mentions and rejects ambient text."""
    # Valid wake-word triggers
    assert userbot_bot._whisper_confirms_indio("che indio ponete un tema") is True
    assert userbot_bot._whisper_confirms_indio("hola indio que tal") is True
    assert userbot_bot._whisper_confirms_indio("indio, que hora es") is True
    assert userbot_bot._whisper_confirms_indio("INDIO DALE") is True

    # Invalid ambient text without wake-word (VOSK false positives)
    assert userbot_bot._whisper_confirms_indio("hola como andan todos") is False
    assert userbot_bot._whisper_confirms_indio("vamos a jugar una partida") is False
    assert userbot_bot._whisper_confirms_indio("el individuo caminaba por la calle") is False
    assert userbot_bot._whisper_confirms_indio("") is False
