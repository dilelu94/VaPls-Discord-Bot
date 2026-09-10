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
