import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import bot
import jkanime


@pytest.mark.asyncio
async def test_stream_jkanime_direct_episode_url(ctx_factory):
    ctx = ctx_factory()
    ctx.guild_id = 12345
    ctx.guild.get_channel = MagicMock(return_value=None)
    ctx.author.voice = MagicMock()
    ctx.author.voice.channel = MagicMock()
    ctx.author.voice.channel.id = 9999
    ctx.author.voice.channel.name = "General"

    ep_url = "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/1/"
    extracted_stream = "https://nika.playmudos.com/test_jojo.m3u8"

    with patch.object(
        jkanime,
        "extract_jkanime_stream",
        AsyncMock(return_value=(extracted_stream, "JoJo Stone Ocean - Episodio 1")),
    ), patch.object(
        bot,
        "start_iptv_stream_logic",
        AsyncMock(return_value=(True, "📺 Transmitiendo JoJo", False)),
    ):
        await bot.stream(ctx, canal=ep_url)

        assert bot._active_sources.get(12345) == {
            "type": "jkanime",
            "url": extracted_stream,
        }
        assert len(ctx.sent_messages) > 0


@pytest.mark.asyncio
async def test_stream_jkanime_anime_main_url(ctx_factory):
    ctx = ctx_factory()
    ctx.guild_id = 12345
    ctx.guild.get_channel = MagicMock(return_value=None)
    ctx.author.voice = MagicMock()
    ctx.author.voice.channel = MagicMock()

    main_url = "https://jkanime.net/jojo-no-kimyou-na-bouken-part-6-stone-ocean/"
    mock_info = {
        "title": "JoJo Stone Ocean",
        "slug": "jojo-no-kimyou-na-bouken-part-6-stone-ocean",
        "url": main_url,
        "image": "https://cdn.jkdesa.com/test.jpg",
        "episodes": 12,
    }

    with patch.object(
        jkanime, "get_jkanime_anime_info", AsyncMock(return_value=mock_info)
    ):
        await bot.stream(ctx, canal=main_url)
        assert ctx.interaction.edit_original_response.called is True


@pytest.mark.asyncio
async def test_stream_jkanime_fallback_search(ctx_factory):
    ctx = ctx_factory()
    ctx.guild_id = 12345
    ctx.guild.get_channel = MagicMock(return_value=None)
    ctx.author.voice = MagicMock()
    ctx.author.voice.channel = MagicMock()

    mock_search = [
        {
            "title": "Chainsaw Man",
            "slug": "chainsaw-man",
            "url": "https://jkanime.net/chainsaw-man/",
            "image": "https://cdn.jkdesa.com/csm.jpg",
        }
    ]

    with patch.object(bot.iptv, "search", AsyncMock(return_value=[])), patch.object(
        jkanime, "search_jkanime", AsyncMock(return_value=mock_search)
    ):
        await bot.stream(ctx, canal="chainsaw man")
        assert ctx.interaction.edit_original_response.called is True


def test_parse_stream_query():
    assert bot.parse_stream_query("https://steamcommunity.com/id/FrankOxx/ 6") == (
        "https://steamcommunity.com/id/FrankOxx/",
        360.0,
    )
    assert bot.parse_stream_query("https://youtube.com/watch?v=123 10") == (
        "https://youtube.com/watch?v=123",
        600.0,
    )
    assert bot.parse_stream_query("jojo part 6 cap 13 min 5") == (
        "jojo part 6 cap 13",
        300.0,
    )
    assert bot.parse_stream_query("chainsaw man") == ("chainsaw man", 0.0)
