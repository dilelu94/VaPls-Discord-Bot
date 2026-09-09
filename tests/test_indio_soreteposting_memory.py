"""Behavioral tests for Indio reading chat messages in #soreteposting.

Requirements:
- Messages from #soreteposting are recorded into Indio's short-term history.
- Messages from non-#soreteposting channels are NOT recorded.
- Web URLs / links are stripped from sanitized history.
- Link-only messages are ignored completely.
- Command messages (starting with /, !, ., $, ?) are ignored completely.
- Bot/system accounts are ignored.
"""

import types
import pytest
from unittest.mock import MagicMock, AsyncMock

import config
import geminiCommand


def _make_mock_message(content: str, channel_id: int = 451580655650996236, channel_name: str = "soreteposting", author_id: int = 12345, author_name: str = "Diego", is_bot: bool = False, guild_id: int = 999):
    author = types.SimpleNamespace(
        id=author_id,
        display_name=author_name,
        name=author_name,
        bot=is_bot,
    )
    channel = types.SimpleNamespace(
        id=channel_id,
        name=channel_name,
    )
    guild = types.SimpleNamespace(id=guild_id)
    return types.SimpleNamespace(
        content=content,
        author=author,
        channel=channel,
        guild=guild,
    )


def test_sanitize_for_history_strips_urls():
    """_sanitize_for_history should remove http, https, and www links."""
    text1 = "Mira este video https://youtu.be/xyz123 esta zarpado"
    assert "https://" not in geminiCommand._sanitize_for_history(text1)
    assert geminiCommand._sanitize_for_history(text1) == "Mira este video esta zarpado"

    text2 = "Entra a www.google.com para ver la data"
    assert "www.google.com" not in geminiCommand._sanitize_for_history(text2)
    assert geminiCommand._sanitize_for_history(text2) == "Entra a para ver la data"

    text3 = "https://example.com/test"
    assert geminiCommand._sanitize_for_history(text3) == ""


def test_is_command_message():
    """is_command_message should identify prefix commands."""
    assert geminiCommand.is_command_message("/play despacito") is True
    assert geminiCommand.is_command_message("!help") is True
    assert geminiCommand.is_command_message(".cmd test") is True
    assert geminiCommand.is_command_message("$price BTC") is True
    assert geminiCommand.is_command_message("?query") is True

    assert geminiCommand.is_command_message("hola indio como va") is False
    assert geminiCommand.is_command_message("che miren esto / no es comando") is False


def test_is_soreteposting_channel():
    """_is_soreteposting_channel should return True for story channel or soreteposting in name."""
    ch1 = types.SimpleNamespace(id=config.INDIO_STORY_CHANNEL_ID, name="general")
    assert geminiCommand._is_soreteposting_channel(ch1) is True

    ch2 = types.SimpleNamespace(id=8888, name="soreteposting")
    assert geminiCommand._is_soreteposting_channel(ch2) is True

    ch3 = types.SimpleNamespace(id=8888, name="sorete-posting")
    assert geminiCommand._is_soreteposting_channel(ch3) is True

    ch4 = types.SimpleNamespace(id=7777, name="musica")
    assert geminiCommand._is_soreteposting_channel(ch4) is False


@pytest.mark.asyncio
async def test_record_soreteposting_chat_message_success(monkeypatch):
    """Normal text message in soreteposting should be saved into _indio_history."""
    monkeypatch.setattr(geminiCommand, "_indio_history", {})
    monkeypatch.setattr(geminiCommand, "_indio_locks", {})
    monkeypatch.setattr(geminiCommand, "_persist_indio_state", AsyncMock(return_value=None))

    msg = _make_mock_message("hoy juega la seleccion")
    res = await geminiCommand.record_soreteposting_chat_message(msg)

    assert res is True
    hist_key = "guild-999"
    assert hist_key in geminiCommand._indio_history
    history = geminiCommand._indio_history[hist_key]
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["parts"][0]["text"] == "Diego: hoy juega la seleccion"


@pytest.mark.asyncio
async def test_record_soreteposting_chat_message_ignores_commands(monkeypatch):
    """Command messages in soreteposting should NOT be saved to history."""
    monkeypatch.setattr(geminiCommand, "_indio_history", {})
    monkeypatch.setattr(geminiCommand, "_indio_locks", {})

    msg1 = _make_mock_message("/play despacito")
    res1 = await geminiCommand.record_soreteposting_chat_message(msg1)
    assert res1 is False

    msg2 = _make_mock_message("!soundpad risa")
    res2 = await geminiCommand.record_soreteposting_chat_message(msg2)
    assert res2 is False

    assert len(geminiCommand._indio_history) == 0


@pytest.mark.asyncio
async def test_record_soreteposting_chat_message_strips_links_and_ignores_link_only(monkeypatch):
    """Links should be stripped and link-only messages ignored."""
    monkeypatch.setattr(geminiCommand, "_indio_history", {})
    monkeypatch.setattr(geminiCommand, "_indio_locks", {})
    monkeypatch.setattr(geminiCommand, "_persist_indio_state", AsyncMock(return_value=None))

    # Link-only message
    msg_link = _make_mock_message("https://youtube.com/watch?v=123")
    res_link = await geminiCommand.record_soreteposting_chat_message(msg_link)
    assert res_link is False

    # Text + link message
    msg_mixed = _make_mock_message("miren esta foto https://imgur.com/xyz.jpg tremenda")
    res_mixed = await geminiCommand.record_soreteposting_chat_message(msg_mixed)
    assert res_mixed is True

    hist_key = "guild-999"
    history = geminiCommand._indio_history[hist_key]
    assert len(history) == 1
    assert "https://" not in history[0]["parts"][0]["text"]
    assert history[0]["parts"][0]["text"] == "Diego: miren esta foto tremenda"


@pytest.mark.asyncio
async def test_record_soreteposting_chat_message_ignores_bots(monkeypatch):
    """Bot messages must be ignored."""
    monkeypatch.setattr(geminiCommand, "_indio_history", {})

    msg = _make_mock_message("hola a todos", is_bot=True)
    res = await geminiCommand.record_soreteposting_chat_message(msg)

    assert res is False
    assert len(geminiCommand._indio_history) == 0
