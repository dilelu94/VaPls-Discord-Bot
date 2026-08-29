"""Behavior tests for Indio TTS voice replies."""
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import geminiCommand
import tts


def test_clean_text_for_speech():
    raw = "Hola **amigo** <@12345> https://example.com <:smile:999> :joy: #encabezado `codigo`"
    cleaned = geminiCommand._clean_text_for_speech(raw)
    assert "amigo" in cleaned
    assert "https://" not in cleaned
    assert "<@" not in cleaned
    assert "<:smile:" not in cleaned
    assert "`codigo`" not in cleaned


@pytest.mark.asyncio
async def test_speak_indio_reply_plays_audio(monkeypatch, tmp_path):
    bot = MagicMock(name="Bot")
    guild = MagicMock(name="Guild")
    fake_vc = MagicMock(name="VoiceClient")
    fake_vc.is_connected.return_value = True
    fake_vc.play = MagicMock()
    guild.voice_client = fake_vc
    bot.get_guild.return_value = guild

    member = MagicMock(name="Member")
    member.voice = SimpleNamespace(channel=MagicMock(id=100))

    fake_wav = str(tmp_path / "indio_reply.wav")
    with open(fake_wav, "wb") as f:
        f.write(b"RIFF....WAVEfmt ....data....")

    monkeypatch.setattr(tts, "generate_tts_wav", lambda text, output_path=None: fake_wav)

    await geminiCommand._speak_indio_reply(bot, 12345, member, "Hola che, respondiendo en voz.")

    assert fake_vc.play.called
