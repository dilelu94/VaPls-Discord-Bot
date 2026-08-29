"""Behavior tests for Indio TTS voice replies."""
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp
import geminiCommand


def test_clean_text_for_speech():
    raw = "Hola **amigo** <@12345> https://example.com <:smile:999> :joy: #encabezado `codigo`"
    cleaned = geminiCommand._clean_text_for_speech(raw)
    assert "amigo" in cleaned
    assert "https://" not in cleaned
    assert "<@" not in cleaned
    assert "<:smile:" not in cleaned
    assert "`codigo`" not in cleaned


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
