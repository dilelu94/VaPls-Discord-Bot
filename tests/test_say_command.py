"""Behavior tests for TTS generation and /say slash command."""
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tts
import sayCommand
import bot


def _make_say_ctx(author_in_voice: bool = True, text_param: str = "Hola prueba"):
    ctx = MagicMock(name="Ctx")
    ctx.guild = MagicMock(name="Guild")
    ctx.guild.id = 12345
    ctx.guild.voice_client = None

    author = MagicMock(name="Author")
    author.display_name = "TestUser"
    if author_in_voice:
        author.voice = SimpleNamespace(channel=MagicMock(id=54321))
    else:
        author.voice = None
    ctx.author = author

    ctx.respond = AsyncMock()
    ctx.defer = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_say_logic_requires_text():
    ctx = _make_say_ctx(text_param="")
    await sayCommand.sayLogic(ctx, "")
    ctx.respond.assert_called_once()
    assert "Debes proporcionar un texto" in ctx.respond.call_args[0][0]


@pytest.mark.asyncio
async def test_say_logic_requires_voice_channel():
    ctx = _make_say_ctx(author_in_voice=False)
    ctx.guild.voice_channels = []
    await sayCommand.sayLogic(ctx, "Hola mundo")
    assert ctx.respond.called
    res = ctx.respond.call_args[0][0]
    assert "canal de voz" in res


@pytest.mark.asyncio
async def test_say_logic_connects_and_plays_tts(monkeypatch, tmp_path):
    ctx = _make_say_ctx(author_in_voice=True)
    fake_vc = MagicMock(name="VoiceClient")
    fake_vc.is_connected.return_value = True
    fake_vc.is_playing.return_value = False
    fake_vc.play = MagicMock()
    ctx.guild.voice_client = fake_vc

    fake_wav = str(tmp_path / "test.wav")
    with open(fake_wav, "wb") as f:
        f.write(b"RIFF....WAVEfmt ....data....")

    monkeypatch.setattr(tts, "generate_tts_wav", lambda text, output_path=None: fake_wav)

    await sayCommand.sayLogic(ctx, "Hola desde test")

    assert fake_vc.play.called
    assert ctx.respond.called
    assert "TestUser" in ctx.respond.call_args[0][0]


def test_ensure_model_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "VOICE_DIR", str(tmp_path))
    monkeypatch.setattr(tts, "MODEL_PATH", str(tmp_path / "model.onnx"))
    monkeypatch.setattr(tts, "CONFIG_PATH", str(tmp_path / "model.json"))

    def fake_retrieve(url, path):
        with open(path, "w") as f:
            f.write("fake_content")

    monkeypatch.setattr("urllib.request.urlretrieve", fake_retrieve)

    assert tts.ensure_model_exists()
    assert os.path.exists(tmp_path / "model.onnx")
    assert os.path.exists(tmp_path / "model.json")
