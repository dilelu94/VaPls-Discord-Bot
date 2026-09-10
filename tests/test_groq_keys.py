"""Unit tests for groqKeys module (DM Groq API key reception and persistence)."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import config
import groqKeys


@pytest.fixture(autouse=True)
def _reset_groq_keys(tmp_path, monkeypatch):
    """Isolate groqKeys state per test."""
    dummy_file = str(tmp_path / "test_groq_keys.json")
    monkeypatch.setattr(config, "GROQ_KEYS_FILE", dummy_file)
    monkeypatch.setattr(groqKeys.config, "GROQ_KEYS_FILE", dummy_file)
    groqKeys._keys.clear()
    yield
    groqKeys._keys.clear()


def test_extract_keys_from_text():
    """Verify extract_keys_from_text correctly extracts gsk_... tokens."""
    raw = "Hola Indio acá tenés mi key gsk_1234567890abcdef1234567890abcdef1234567890abcdef y otra gsk_abcdef1234567890abcdef1234567890abcdef1234567890"
    extracted = groqKeys.extract_keys_from_text(raw)
    assert len(extracted) == 2
    assert extracted[0] == "gsk_1234567890abcdef1234567890abcdef1234567890abcdef"
    assert extracted[1] == "gsk_abcdef1234567890abcdef1234567890abcdef1234567890"

    # Invalid text without keys
    assert groqKeys.extract_keys_from_text("hola que tal") == []


@pytest.mark.asyncio
async def test_add_key_and_persistence(tmp_path):
    """Verify add_key persists to disk and updates config.GROQ_API_KEY."""
    valid_key = "gsk_validkey1234567890abcdef1234567890abcdef123456"
    ok, reason = await groqKeys.add_key(
        valid_key,
        owner_id="12345",
        owner_name="TestUser",
        source="dm:test",
    )
    assert ok is True
    assert reason == "added"
    assert config.GROQ_API_KEY == valid_key

    # Check persistence on disk
    file_path = getattr(config, "GROQ_KEYS_FILE")
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data["keys"]) == 1
    assert data["keys"][0]["key"] == valid_key
    assert data["keys"][0]["owner_name"] == "TestUser"

    # Duplicate key rejection
    ok_dupe, reason_dupe = await groqKeys.add_key(
        valid_key,
        owner_id="12345",
        owner_name="TestUser",
        source="dm:test",
    )
    assert ok_dupe is False
    assert reason_dupe == "already in pool"


@pytest.mark.asyncio
async def test_userbot_handle_groq_key_dm(monkeypatch):
    """Verify _handle_groq_key_dm in userbot/bot.py extracts keys, updates pool, and replies."""
    import sys
    import types

    if "discord.ext.voice_recv" not in sys.modules:
        sys.modules["discord.ext.voice_recv"] = MagicMock()
    if "discord.voice_state" not in sys.modules:
        mock_vs = types.ModuleType("discord.voice_state")
        mock_vs.VoiceState = MagicMock()
        mock_vs.VoiceConnectionState = MagicMock()
        sys.modules["discord.voice_state"] = mock_vs
        import discord
        discord.voice_state = mock_vs

    from userbot.bot import _handle_groq_key_dm

    valid_key = "gsk_userbotdmtest1234567890abcdef1234567890abcdef123"
    message = AsyncMock()
    message.guild = None
    message.content = f"Hola indio acá va gsk_key: {valid_key}"
    message.author.id = 99999
    message.author.display_name = "Miles"

    handled = await _handle_groq_key_dm(message)
    assert handled is True
    assert config.GROQ_API_KEY == valid_key
    message.channel.send.assert_called_once()
    sent_text = message.channel.send.call_args[0][0]
    assert "⚡ Sumé 1 Groq API key(s) al pool" in sent_text


@pytest.mark.asyncio
async def test_key_rotation_and_cooldown():
    """Verify get_next_groq_key rotates keys and respects cooldowns."""
    key1 = "gsk_key11111111111111111111111111111111111111111111"
    key2 = "gsk_key22222222222222222222222222222222222222222222"
    key3 = "gsk_key33333333333333333333333333333333333333333333"

    await groqKeys.add_key(key1, owner_id="1", owner_name="User1", source="test")
    await groqKeys.add_key(key2, owner_id="2", owner_name="User2", source="test")
    await groqKeys.add_key(key3, owner_id="3", owner_name="User3", source="test")

    # Round robin
    k_a = groqKeys.get_next_groq_key()
    k_b = groqKeys.get_next_groq_key()
    k_c = groqKeys.get_next_groq_key()
    assert {k_a, k_b, k_c} == {key1, key2, key3}

    # Cooldown key1
    groqKeys.mark_key_cooldown(k_a, 60.0)
    for _ in range(5):
        nxt = groqKeys.get_next_groq_key()
        assert nxt != k_a, "Key in cooldown should not be selected"

