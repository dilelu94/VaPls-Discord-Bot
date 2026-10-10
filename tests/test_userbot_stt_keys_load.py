"""Behavioral tests verifying that userbot STT key managers load keys from disk
regardless of the working directory, and reload lazily when the in-memory pool is empty."""

import asyncio
import json
import os
import sys
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

from userbot.bot import _run_gemini_stt, _run_groq_stt, _transcribe_pcm


def _dummy_pcm(duration_sec: float = 0.5) -> bytes:
    return b"\x00\x00" * int(16000 * duration_sec)


class MockAsyncContext:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *args):
        return None


@pytest.fixture(autouse=True)
def clean_keys_pools():
    geminiKeys._keys.clear()
    geminiKeys._key_cooldowns.clear()
    groqKeys._keys.clear()
    groqKeys._key_cooldowns.clear()
    yield
    geminiKeys._keys.clear()
    geminiKeys._key_cooldowns.clear()
    groqKeys._keys.clear()
    groqKeys._key_cooldowns.clear()


def test_gemini_keys_load_from_disk_resolves_when_in_subdirectory(monkeypatch, tmp_path):
    """When the process runs with cwd inside a subfolder (like userbot/),
    geminiKeys.load_from_disk() still locates the file in the repo root."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sub_dir = repo_dir / "userbot"
    sub_dir.mkdir()

    keys_file = repo_dir / "gemini_keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"key": "AIzaFakeKeyFromRoot", "owner_name": "Test"}]}),
        encoding="utf-8",
    )

    # Simulate geminiKeys module living in repo_dir
    fake_gemini_file = str(repo_dir / "geminiKeys.py")
    monkeypatch.setattr(geminiKeys, "__file__", fake_gemini_file)
    monkeypatch.setattr(config, "GEMINI_KEYS_FILE", "gemini_keys.json")

    # Change working directory to the subfolder
    orig_cwd = os.getcwd()
    os.chdir(sub_dir)
    try:
        count = geminiKeys.load_from_disk()
        assert count == 1
        assert "AIzaFakeKeyFromRoot" in geminiKeys.active_keys()
    finally:
        os.chdir(orig_cwd)


def test_groq_keys_load_from_disk_resolves_when_in_subdirectory(monkeypatch, tmp_path):
    """When the process runs with cwd inside a subfolder,
    groqKeys.load_from_disk() still locates the data/ file in the repo root."""
    repo_dir = tmp_path / "repo"
    data_dir = repo_dir / "data"
    data_dir.mkdir(parents=True)
    sub_dir = repo_dir / "userbot"
    sub_dir.mkdir()

    keys_file = data_dir / "groq_keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"key": "gsk_test_fake_key_1234567890abcdef", "owner_name": "Test"}]}),
        encoding="utf-8",
    )

    fake_groq_file = str(repo_dir / "groqKeys.py")
    monkeypatch.setattr(groqKeys, "__file__", fake_groq_file)
    monkeypatch.setattr(config, "GROQ_KEYS_FILE", "data/groq_keys.json")

    orig_cwd = os.getcwd()
    os.chdir(sub_dir)
    try:
        count = groqKeys.load_from_disk()
        assert count == 1
        assert "gsk_test_fake_key_1234567890abcdef" in groqKeys.active_keys()
    finally:
        os.chdir(orig_cwd)


@pytest.mark.asyncio
async def test_run_gemini_stt_lazy_reloads_when_keys_empty(monkeypatch):
    """If in-memory keys were not loaded yet, _run_gemini_stt reloads from disk before failing."""
    assert len(geminiKeys.active_keys()) == 0

    reloaded = False

    def fake_load():
        nonlocal reloaded
        reloaded = True
        geminiKeys._keys.append({"key": "AIzaLazyLoadedKey"})
        return 1

    monkeypatch.setattr(geminiKeys, "load_from_disk", fake_load)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "che indio hola"}]}}]
    })
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=MockAsyncContext(mock_resp))

    with patch("userbot.bot._get_http", AsyncMock(return_value=mock_session)):
        res = await _run_gemini_stt(_dummy_pcm(1.0))
        assert reloaded is True
        assert res == "che indio hola"


@pytest.mark.asyncio
async def test_run_groq_stt_lazy_reloads_when_keys_empty(monkeypatch):
    """If in-memory keys were not loaded yet, _run_groq_stt reloads from disk before failing."""
    assert len(groqKeys.active_keys()) == 0

    reloaded = False

    def fake_load():
        nonlocal reloaded
        reloaded = True
        groqKeys._keys.append({"key": "gsk_lazy_loaded_groq_key_1234567"})
        return 1

    monkeypatch.setattr(groqKeys, "load_from_disk", fake_load)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"text": "che indio hola desde groq"})
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=MockAsyncContext(mock_resp))

    with patch("userbot.bot._get_http", AsyncMock(return_value=mock_session)):
        res = await _run_groq_stt(_dummy_pcm(1.0))
        assert reloaded is True
        assert res == "che indio hola desde groq"


@pytest.mark.asyncio
async def test_transcribe_pcm_reloads_keys_and_transcribes(monkeypatch):
    """_transcribe_pcm ensures keys are loaded even if starting with empty pools."""
    assert len(geminiKeys.active_keys()) == 0

    def fake_gemini_load():
        geminiKeys._keys.append({"key": "AIzaWorkingKey"})
        return 1

    monkeypatch.setattr(geminiKeys, "load_from_disk", fake_gemini_load)

    with patch("userbot.bot._run_gemini_stt", AsyncMock(return_value="che indio qué onda")) as mock_gemini:
        res = await _transcribe_pcm(_dummy_pcm(1.0))
        assert res == "che indio qué onda"
        mock_gemini.assert_called_once()
