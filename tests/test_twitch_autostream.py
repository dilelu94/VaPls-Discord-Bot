import pytest
from unittest.mock import AsyncMock, PropertyMock
import bot
import config


def get_sent_message(ctx) -> str:
    calls = (
        getattr(ctx.respond, "call_args_list", None)
        or getattr(ctx.followup.send, "call_args_list", None)
        or getattr(ctx.interaction.edit_original_response, "call_args_list", None)
    )
    if not calls:
        return ""
    call = calls[-1]
    args, kwargs = call.args, call.kwargs
    if kwargs.get("content"):
        return str(kwargs["content"])
    if kwargs.get("embed"):
        return str(kwargs["embed"].title or kwargs["embed"].description or "")
    if args:
        return str(args[0])
    return ""


def test_canonical_twitch_url():
    """Test parsing and canonicalization of Twitch channel URLs and schedule links."""
    assert bot._canonical_twitch_url("https://www.twitch.tv/soyverycherrii/schedule") == "https://www.twitch.tv/soyverycherrii"
    assert bot._canonical_twitch_url("https://www.twitch.tv/soyverycherrii/") == "https://www.twitch.tv/soyverycherrii"
    assert bot._canonical_twitch_url("soyverycherrii") == "https://www.twitch.tv/soyverycherrii"
    assert bot._extract_twitch_channel_name("https://www.twitch.tv/soyverycherrii") == "soyverycherrii"


@pytest.mark.asyncio
async def test_autostream_add_and_remove(ctx_factory):
    """Test adding, listing, and removing Twitch channels via /stream slash command options."""
    ctx = ctx_factory(channel_id=config.INDIO_PLAY_CHANNEL_ID)

    # Reset monitored channels
    bot._autostream_monitored_channels.clear()

    # Add channel via /stream canal: ... accion: add
    await bot.stream(ctx, canal="https://www.twitch.tv/soyverycherrii/schedule", accion="add")
    assert "https://www.twitch.tv/soyverycherrii" in bot._autostream_monitored_channels
    assert "agregado" in get_sent_message(ctx).lower()

    # Add duplicate
    ctx_dup = ctx_factory(channel_id=config.INDIO_PLAY_CHANNEL_ID)
    await bot.stream(ctx_dup, canal="soyverycherrii", accion="add")
    assert len(bot._autostream_monitored_channels) == 1
    assert "ya está en la lista" in get_sent_message(ctx_dup).lower()

    # List channels via /stream canal: list
    ctx_list = ctx_factory(channel_id=config.INDIO_PLAY_CHANNEL_ID)
    await bot.stream(ctx_list, canal="list")
    assert get_sent_message(ctx_list) != ""

    # Remove channel via /stream canal: ... accion: remove
    ctx_rem = ctx_factory(channel_id=config.INDIO_PLAY_CHANNEL_ID)
    await bot.stream(ctx_rem, canal="soyverycherrii", accion="remove")
    assert "https://www.twitch.tv/soyverycherrii" not in bot._autostream_monitored_channels
    assert "eliminado" in get_sent_message(ctx_rem).lower()


@pytest.mark.asyncio
async def test_twitch_autostream_monitor_live_transition(monkeypatch):
    """Test that twitch_autostream_monitor starts stream when channel goes live, and stops stream when offline."""
    bot._autostream_monitored_channels = ["https://www.twitch.tv/soyverycherrii"]
    bot._autostream_active_streams.clear()

    monkeypatch.setattr(config, "TWITCH_AUTOSTREAM_ENABLED", True)

    start_mock = AsyncMock(return_value=(True, "Stream iniciado", True))
    stop_mock = AsyncMock(return_value=(True, "Stream detenido"))

    monkeypatch.setattr(bot, "start_iptv_stream_logic", start_mock)
    monkeypatch.setattr(bot, "stop_stream_for_guild", stop_mock)

    # 1. Channel is LIVE
    async def mock_check_live(url):
        return True, "En vivo en Twitch!"

    monkeypatch.setattr(bot, "_check_twitch_live", mock_check_live)

    # Create dummy bot guilds with a voice channel
    class DummyGuild:
        id = 999
        afk_channel = None
        voice_channels = []

    class DummyVC:
        id = 12345
        name = "General Voice"
        guild = DummyGuild()

        class Member:
            bot = False

        members = [Member(), Member()]

    dummy_guild = DummyGuild()
    dummy_vc = DummyVC()
    dummy_vc.guild = dummy_guild
    dummy_guild.voice_channels = [dummy_vc]

    monkeypatch.setattr(type(bot.bot), "guilds", PropertyMock(return_value=[dummy_guild]))

    # Run monitor loop logic once
    await bot.twitch_autostream_monitor()

    # Verify stream started
    assert start_mock.called
    assert "https://www.twitch.tv/soyverycherrii" in bot._autostream_active_streams

    # 2. Channel goes OFFLINE
    async def mock_check_offline(url):
        return False, ""

    monkeypatch.setattr(bot, "_check_twitch_live", mock_check_offline)

    # Run monitor loop logic again
    await bot.twitch_autostream_monitor()

    # Verify stream stopped
    assert stop_mock.called
    assert "https://www.twitch.tv/soyverycherrii" not in bot._autostream_active_streams
