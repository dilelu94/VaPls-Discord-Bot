import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import bot
from bot import (
    _QUACK_COOLDOWN,
    _active_sources,
    _is_quack_effect,
    _last_quack_time,
    _paused_streams,
    on_voice_channel_effect_send,
)


def test_is_quack_effect_various_payloads():
    # 1. Sound object with name="quack"
    evt1 = MagicMock()
    evt1.sound.name = "Quack"
    assert _is_quack_effect(evt1) is True

    # 2. PartialSoundboardSound without .name attribute but id=1
    evt2 = MagicMock(spec=["sound", "guild", "data"])
    evt2.sound = type("PartialSound", (), {"id": 1})()
    assert _is_quack_effect(evt2) is True

    # 3. Raw data sound_name="quack"
    evt3 = MagicMock()
    evt3.sound = None
    evt3.data = {"sound_name": "quack"}
    assert _is_quack_effect(evt3) is True

    # 4. Raw data sound_id=1
    evt4 = MagicMock()
    evt4.sound = None
    evt4.data = {"sound_id": "1"}
    assert _is_quack_effect(evt4) is True

    # 5. Emoji duck
    evt5 = MagicMock()
    evt5.sound = None
    evt5.data = {}
    evt5.emoji.name = "🦆"
    assert _is_quack_effect(evt5) is True

    # 6. Non-quack sound
    evt6 = MagicMock()
    evt6.sound.name = "fart"
    evt6.sound.id = 99
    evt6.data = {"sound_name": "fart", "sound_id": "99"}
    evt6.emoji = None
    assert _is_quack_effect(evt6) is False


@pytest.mark.asyncio
async def test_on_voice_channel_effect_send_toggle_and_cooldown():
    guild_id = 98765
    _active_sources[guild_id] = {"type": "iptv", "url": "http://example.com/live.m3u8"}
    _paused_streams.discard(guild_id)
    _last_quack_time.pop(guild_id, None)

    evt = MagicMock()
    evt.guild.id = guild_id
    evt.sound.name = "quack"

    with patch("bot._send_stream_control", new_callable=AsyncMock) as mock_control:
        mock_control.return_value = True

        # First quack -> pauses stream
        await on_voice_channel_effect_send(evt)
        mock_control.assert_called_once_with(guild_id, "pause")
        assert guild_id in _paused_streams

        # Spammed quack immediately after (< _QUACK_COOLDOWN) -> ignored
        mock_control.reset_mock()
        await on_voice_channel_effect_send(evt)
        mock_control.assert_not_called()

        # Quack after cooldown elapsed -> resumes stream
        _last_quack_time[guild_id] = time.time() - (_QUACK_COOLDOWN + 0.1)
        await on_voice_channel_effect_send(evt)
        mock_control.assert_called_once_with(guild_id, "resume")
        assert guild_id not in _paused_streams

    # Cleanup
    _active_sources.pop(guild_id, None)
    _paused_streams.discard(guild_id)
    _last_quack_time.pop(guild_id, None)
