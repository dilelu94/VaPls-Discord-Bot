"""Tests for JoJo reference detection in media_analyzer.py and bot.py integration."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import media_analyzer
import bot


@pytest.mark.asyncio
async def test_check_jojo_reference_text():
    # Keywords matches
    res1 = await media_analyzer.check_jojo_reference("Seba", "texto", text="Che, me parece que esto es za warudo!")
    assert res1 is not None
    assert "za warudo" in res1.lower()

    res2 = await media_analyzer.check_jojo_reference("Caro", "texto", text="kono dio da!")
    assert res2 is not None
    assert "kono dio da" in res2.lower()


@pytest.mark.asyncio
async def test_check_jojo_reference_subtle_pose():
    mock_reply_tree = MagicMock()
    mock_reply_tree.text = "Una rama torcida que parece un personaje de JoJo posando dramáticamente"

    with patch("geminiClient.generate", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_reply_tree
        res = await media_analyzer.check_jojo_reference("Seba", "imagen", image_bytes=b"\xff\xd8\xfffake_tree")
        assert res == "Una rama torcida que parece un personaje de JoJo posando dramáticamente"


@pytest.mark.asyncio
async def test_check_jojo_reference_link():
    meta = {
        "title": "[Anime] JoJo's Bizarre Adventure Part 6 Stone Ocean Episode 12",
        "description": "Episode 12 of JoJo",
        "tags": ["jojo", "anime"],
    }
    res = await media_analyzer.check_jojo_reference("Seba", "enlace", link_meta=meta)
    assert res is not None
    assert "JoJo's Bizarre Adventure" in res


@pytest.mark.asyncio
async def test_check_jojo_reference_vision():
    mock_reply_yes = MagicMock()
    mock_reply_yes.text = "Meme de Dio Brando Kono DIO Da"

    with patch("geminiClient.generate", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_reply_yes
        res = await media_analyzer.check_jojo_reference("Seba", "imagen", image_bytes=b"\xff\xd8\xfffake")
        assert res == "Meme de Dio Brando Kono DIO Da"

    mock_reply_no = MagicMock()
    mock_reply_no.text = "NO"

    with patch("geminiClient.generate", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_reply_no
        res = await media_analyzer.check_jojo_reference("Seba", "imagen", image_bytes=b"\xff\xd8\xfffake")
        assert res is None


@pytest.mark.asyncio
async def test_analyze_message_media_and_links_triggers_jojo_reply():
    msg = MagicMock()
    msg.guild = MagicMock(id=123456789)
    msg.author = MagicMock(id=987654321, display_name="Seba", bot=False)
    msg.content = "No te la puedo creer, za warudo total esto"
    msg.attachments = []
    msg.channel = MagicMock(id=111222333)

    # Roll low (< 0.05), so reply triggers
    with patch("random.random", return_value=0.01):
        with patch("geminiCommand.askIndio", new_callable=AsyncMock) as mock_ask:
            await bot._analyze_message_media_and_links(msg)
            assert mock_ask.called
            call_args = mock_ask.call_args
            assert "JOJO" in call_args[0][1] or "JoJo" in call_args[0][1]


@pytest.mark.asyncio
async def test_analyze_message_media_and_links_skips_jojo_reply_on_random_roll():
    msg = MagicMock()
    msg.guild = MagicMock(id=123456789)
    msg.author = MagicMock(id=987654321, display_name="Seba", bot=False)
    msg.content = "No te la puedo creer, za warudo total esto"
    msg.attachments = []
    msg.channel = MagicMock(id=111222333)

    # Roll high (> 0.05), so reply is skipped
    with patch("random.random", return_value=0.95):
        with patch("geminiCommand.askIndio", new_callable=AsyncMock) as mock_ask:
            await bot._analyze_message_media_and_links(msg)
            assert not mock_ask.called


def test_jojo_reply_probability_default_value():
    import config
    assert config.JOJO_REPLY_PROBABILITY == 0.05

