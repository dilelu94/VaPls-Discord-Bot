"""Tests for golive/golive_watcher.py."""

import struct
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from golive.golive_watcher import GoLiveWatcherConnection


@pytest.mark.asyncio
async def test_watcher_connect_and_disconnect():
    mock_bot = MagicMock()
    mock_bot.ws.send_as_json = AsyncMock()

    mock_vc = MagicMock()
    mock_vc.session_id = "test_session_id"
    mock_vc.user.id = 9999

    watcher = GoLiveWatcherConnection(
        bot=mock_bot,
        guild_id=111,
        channel_id=222,
        target_user_id=333,
        vc=mock_vc,
    )

    ok = await watcher.connect()
    assert ok is True
    assert watcher._connected is True

    # Check WebSocket opcodes sent
    assert mock_bot.ws.send_as_json.call_count == 2
    op20_call = mock_bot.ws.send_as_json.call_args_list[0][0][0]
    assert op20_call["op"] == 20
    assert op20_call["d"]["stream_key"] == "guild:111:222:333"

    op22_call = mock_bot.ws.send_as_json.call_args_list[1][0][0]
    assert op22_call["op"] == 22
    assert op22_call["d"]["paused"] is False

    await watcher.disconnect()
    assert watcher._connected is False


@pytest.mark.asyncio
async def test_watcher_on_udp_packet():
    mock_bot = MagicMock()
    mock_vc = MagicMock()

    watcher = GoLiveWatcherConnection(
        bot=mock_bot,
        guild_id=111,
        channel_id=222,
        target_user_id=333,
        vc=mock_vc,
    )

    watcher.receiver.process_rtp_packet = MagicMock()

    # Make video RTP packet with Payload Type 101
    hdr = struct.pack("!BBHII", 0x80, 101, 1, 100, 555)
    rtp_data = hdr + b"\x07\x42"

    watcher._on_udp_packet(rtp_data)
    assert watcher.receiver.process_rtp_packet.call_count == 1
