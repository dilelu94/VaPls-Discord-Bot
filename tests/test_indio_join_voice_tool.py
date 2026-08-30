"""Behavior: Indio join_voice tool triggers userbot voice relay /join."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import geminiCommand


@pytest.mark.asyncio
async def test_join_voice_dispatches_relay():
    mock_bot = MagicMock()
    mock_guild = MagicMock()
    mock_guild.id = 12345
    mock_bot.get_guild.return_value = mock_guild

    mock_channel = MagicMock()
    mock_channel.id = 9999
    mock_channel.name = "General Voice"

    mock_member = MagicMock()
    mock_member.voice.channel = mock_channel

    with patch("geminiCommand._relay_join_to_userbot", AsyncMock(return_value=True)) as mock_join_relay:
        statuses = await geminiCommand._dispatch_indio_actions(
            bot=mock_bot,
            guild_id=12345,
            actions=[("JOIN_VOICE", "")],
            requester_member=mock_member,
        )
        assert statuses == ["join_voice: ok"]
        mock_join_relay.assert_awaited_once_with(9999)
