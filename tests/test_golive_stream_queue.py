import pytest
from unittest.mock import AsyncMock, MagicMock
import golive.bot as golive_bot


@pytest.mark.asyncio
async def test_golive_stream_has_start_players_method():
    """Verify GoLiveStream has _start_players method and can be called without AttributeError."""
    stream = golive_bot.GoLiveStream(
        bot=MagicMock(),
        guild_id=123,
        channel_id=456,
        vc=MagicMock(),
        url="http://example.com/video.mp4",
        title="Test Video",
    )

    assert hasattr(stream, "_start_players")
    # Calling with no conn logs an error and returns gracefully without crashing
    stream.conn = None
    await stream._start_players()
