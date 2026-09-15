import sys
from unittest.mock import MagicMock
import pytest

import discord
if not hasattr(discord, "voice_state"):
    discord.voice_state = MagicMock()
if "discord.voice_state" not in sys.modules:
    sys.modules["discord.voice_state"] = discord.voice_state

for _mod in ("video_compat", "davey_compat", "golive_connection"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from golive.slopsoil.golive import GoLiveConnection, _send_json_safe


@pytest.mark.asyncio
async def test_disconnect_sends_stream_delete_sync_ws():
    mock_bot = MagicMock()
    mock_vc = MagicMock()
    mock_ws = MagicMock()
    # Simulate synchronous send_as_json (returns None, not awaitable)
    sent_ops = []

    def mock_send_as_json(data):
        sent_ops.append(data.get("op"))
        return None

    mock_ws.send_as_json = mock_send_as_json
    mock_bot.ws = mock_ws

    conn = GoLiveConnection(
        bot=mock_bot,
        vc=mock_vc,
        guild_id=123456789,
        channel_id=987654321,
    )
    conn._stream_key = "guild:123456789:987654321:111"

    await conn.disconnect()

    assert 22 in sent_ops  # _OP_STREAM_SET_PAUSED
    assert 19 in sent_ops  # _OP_STREAM_DELETE


@pytest.mark.asyncio
async def test_golive_stream_stop_cleans_up_trackers(monkeypatch):
    from golive.bot import GoLiveStream, _active_streams, client

    stream = GoLiveStream(
        bot=MagicMock(),
        guild_id=123,
        channel_id=456,
        url="http://test.url",
        vc=None,
    )
    _active_streams[123] = stream
    mock_conn = MagicMock()
    mock_conn.disconnect = MagicMock(return_value=None)
    async def fake_disconnect():
        pass
    mock_conn.disconnect = fake_disconnect
    stream.conn = mock_conn

    getattr(client, "live_connections", {})[123] = mock_conn

    await stream.stop(disconnect_voice=False)

    assert stream._stopped is True
    assert 123 not in _active_streams
    assert 123 not in getattr(client, "live_connections", {})

