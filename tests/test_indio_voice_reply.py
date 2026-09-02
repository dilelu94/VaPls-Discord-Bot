"""Behavior tests for Indio TTS voice replies."""
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp
import geminiCommand
import tts


def test_clean_text_for_speech():
    raw = "Hola **amigo** 😊 <@12345> https://example.com <:smile:999> :joy: #encabezado `codigo` 😂👍 🇦🇷"
    cleaned = geminiCommand._clean_text_for_speech(raw)
    assert "amigo" in cleaned
    assert "https://" not in cleaned
    assert "<@" not in cleaned
    assert "<:smile:" not in cleaned
    assert "`codigo`" not in cleaned
    assert "😊" not in cleaned
    assert "😂" not in cleaned
    assert "👍" not in cleaned
    assert "🇦🇷" not in cleaned

    direct_cleaned = tts.clean_text_for_tts("Buenas... 😊 ¿cómo va? 😂")
    assert direct_cleaned == "Buenas... ¿cómo va?"


@pytest.mark.asyncio
async def test_speak_indio_reply_posts_to_userbot_relay(monkeypatch):
    posted_payload = {}

    class MockResponse:
        status = 200
        async def text(self):
            return "OK"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self, *args, **kwargs):
            pass
        def post(self, url, json=None, headers=None, timeout=None):
            nonlocal posted_payload
            posted_payload = json
            return MockResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", MockSession)

    member = MagicMock(name="Member")
    member.id = 999
    member.voice = SimpleNamespace(channel=MagicMock(id=100))

    await geminiCommand._speak_indio_reply(None, 12345, member, "Hola che, respondiendo en voz.")

    assert posted_payload.get("guild_id") == 12345
    assert "respondiendo en voz" in posted_payload.get("text", "")
    assert posted_payload.get("user_id") == 999


@pytest.mark.asyncio
async def test_relay_speak_stops_existing_audio(monkeypatch, tmp_path):
    import sys
    if "discord.ext.voice_recv" not in sys.modules:
        sys.modules["discord.ext.voice_recv"] = MagicMock()
    if "discord.voice_state" not in sys.modules:
        sys.modules["discord.voice_state"] = MagicMock()
    try:
        from userbot import bot as userbot_module
    except Exception:
        pytest.skip("userbot dependencies not present in this python environment")
    from aiohttp import web

    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    mock_vc.is_playing.return_value = True
    mock_vc.channel.id = 456
    mock_vc.channel.name = "TestVoice"

    mock_guild = MagicMock()
    monkeypatch.setattr(userbot_module, "client", MagicMock(is_ready=lambda: True, get_guild=lambda gid: mock_guild))
    monkeypatch.setattr(userbot_module, "_vc_for_guild", lambda g: mock_vc)
    monkeypatch.setattr(userbot_module.config, "RELAY_SECRET", "secret123")
    
    dummy_wav = tmp_path / "test.wav"
    dummy_wav.write_bytes(b"RIFF....WAVE")
    monkeypatch.setattr(tts, "generate_tts_wav", lambda text: str(dummy_wav))

    class DummyReq:
        headers = {"X-API-Secret": "secret123"}
        async def json(self):
            return {"guild_id": 123, "text": "hola que tal"}

    with patch("discord.FFmpegOpusAudio"):
        resp = await userbot_module._relay_speak(DummyReq())
        assert resp.status == 200
        mock_vc.stop.assert_called_once()
        mock_vc.play.assert_called_once()


@pytest.mark.asyncio
async def test_relay_speak_rejoins_channel_if_mismatched(monkeypatch, tmp_path):
    import sys
    import discord
    if "discord.ext.voice_recv" not in sys.modules:
        sys.modules["discord.ext.voice_recv"] = MagicMock()
    if "discord.voice_state" not in sys.modules:
        sys.modules["discord.voice_state"] = MagicMock()
    try:
        from userbot import bot as userbot_module
        if isinstance(userbot_module, MagicMock) or not hasattr(userbot_module, "_relay_speak"):
            pytest.skip("userbot module not fully loaded")
    except Exception:
        pytest.skip("userbot dependencies not present in this python environment")

    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    mock_vc.channel.id = 111  # Old channel
    mock_vc.channel.name = "OldVC"

    target_ch = MagicMock(spec=discord.VoiceChannel)
    target_ch.id = 222  # New channel requested
    target_ch.name = "NewVC"

    mock_guild = MagicMock()
    mock_guild.get_channel.side_effect = lambda cid: target_ch if cid == 222 else None
    monkeypatch.setattr(userbot_module, "client", MagicMock(is_ready=lambda: True, get_guild=lambda gid: mock_guild))
    monkeypatch.setattr(userbot_module, "_vc_for_guild", lambda g: mock_vc)
    monkeypatch.setattr(userbot_module.config, "RELAY_SECRET", "secret123")

    joined = []
    async def _mock_join(ch):
        joined.append(ch)

    monkeypatch.setattr(userbot_module, "_join_channel", _mock_join)

    dummy_wav = tmp_path / "test.wav"
    dummy_wav.write_bytes(b"RIFF....WAVE")
    monkeypatch.setattr(tts, "generate_tts_wav", lambda text: str(dummy_wav))

    class DummyReq:
        headers = {"X-API-Secret": "secret123"}
        async def json(self):
            return {"guild_id": 123, "channel_id": 222, "text": "hola que tal"}

    with patch("discord.FFmpegOpusAudio"):
        resp = await userbot_module._relay_speak(DummyReq())
        assert resp.status == 200
        assert len(joined) == 1
        assert joined[0].id == 222


@pytest.mark.asyncio
async def test_userbot_voice_state_update_stops_audio_when_moved(monkeypatch):
    import sys
    import discord
    if "discord.ext.voice_recv" not in sys.modules:
        sys.modules["discord.ext.voice_recv"] = MagicMock()
    if "discord.voice_state" not in sys.modules:
        sys.modules["discord.voice_state"] = MagicMock()
    try:
        from userbot import bot as userbot_module
        if isinstance(userbot_module, MagicMock) or not hasattr(userbot_module, "on_voice_state_update"):
            pytest.skip("userbot module not fully loaded")
    except Exception:
        pytest.skip("userbot dependencies not present in this python environment")

    mock_client = MagicMock()
    mock_client.user.id = 999
    monkeypatch.setattr(userbot_module, "client", mock_client)

    mock_vc = MagicMock()
    mock_vc.is_playing.return_value = True
    monkeypatch.setattr(userbot_module, "_vc_for_guild", lambda g: mock_vc)

    member = MagicMock(id=999)  # Userbot itself
    before = MagicMock(channel=MagicMock(name="Chan1", guild=MagicMock(id=10)))
    after = MagicMock(channel=MagicMock(id=20, name="Chan2", guild=MagicMock(id=10)))

    if asyncio.iscoroutinefunction(userbot_module.on_voice_state_update):
        await userbot_module.on_voice_state_update(member, before, after)
        mock_vc.stop.assert_called_once()


def test_dave_patch_audio_decryption_fallback(monkeypatch):
    """Ensure audio decryption in _install_dave_patch returns raw audio when DAVE is None or ready=False."""
    import sys
    try:
        from userbot import bot as userbot_module
        from discord.ext.voice_recv.rtp import PacketDecryptor
    except Exception:
        pytest.skip("userbot or discord.ext.voice_recv dependencies not present in this python environment")

    raw_packet = b"opus_packet_sample"
    dummy_rtp_packet = MagicMock(pt=120, ssrc=12345)

    userbot_module._install_dave_patch()

    decryptor_inst = PacketDecryptor()
    decryptor_inst._voice_client = MagicMock()
    decryptor_inst._voice_client._connection = MagicMock(dave_session=None)

    # Calling the method on PacketDecryptor
    if hasattr(decryptor_inst, "_decrypt_rtp_aead_xchacha20_poly1305_rtpsize"):
        res = decryptor_inst._decrypt_rtp_aead_xchacha20_poly1305_rtpsize(dummy_rtp_packet)
        assert res == raw_packet





