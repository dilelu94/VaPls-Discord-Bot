import pytest
from unittest.mock import AsyncMock, PropertyMock
import bot
import config


def test_scheduled_stream_title_default_config():
    """Verify SCHEDULED_STREAM_TITLE default in config."""
    assert config.SCHEDULED_STREAM_TITLE == "Himno Nacional"


@pytest.mark.asyncio
async def test_scheduled_daily_stream_uses_himno_nacional_title(monkeypatch):
    """Test that scheduled_daily_stream passes SCHEDULED_STREAM_TITLE to start_iptv_stream_logic."""
    monkeypatch.setattr(config, "SCHEDULED_STREAM_ENABLED", True)
    monkeypatch.setattr(config, "SCHEDULED_STREAM_TITLE", "Himno Nacional")
    monkeypatch.setattr(bot.scheduled_daily_stream, "_current_loop", 1)

    start_mock = AsyncMock(return_value=(True, "Stream iniciado", True))
    monkeypatch.setattr(bot, "start_iptv_stream_logic", start_mock)

    class DummyGuild:
        id = 1234
        afk_channel = None
        voice_channels = []

    class DummyVC:
        id = 5678
        name = "General Voice"
        guild = DummyGuild()

        class Member:
            bot = False

        members = [Member()]

    dummy_guild = DummyGuild()
    dummy_vc = DummyVC()
    dummy_vc.guild = dummy_guild
    dummy_guild.voice_channels = [dummy_vc]
    dummy_guild.get_channel = lambda cid: dummy_vc if cid == config.SCHEDULED_STREAM_FALLBACK_CHANNEL_ID else None

    monkeypatch.setattr(type(bot.bot), "guilds", PropertyMock(return_value=[dummy_guild]))

    await bot.scheduled_daily_stream()

    assert start_mock.called
    args, _ = start_mock.call_args
    assert args[2] == config.SCHEDULED_STREAM_URL
    assert args[3] == "Himno Nacional"
