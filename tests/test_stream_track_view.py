import pytest
from unittest.mock import MagicMock, AsyncMock
from media_inspector import MediaTracksInfo, AudioTrack, SubtitleTrack
from stream_track_view import StreamTrackSelectView, AudioTrackSelect, SubtitleTrackSelect


@pytest.mark.asyncio
async def test_view_creation_with_multiple_tracks():
    info = MediaTracksInfo(
        url="http://example.com/video.mkv",
        audio_tracks=[
            AudioTrack(index=0, stream_index=1, language="spa", codec="aac"),
            AudioTrack(index=1, stream_index=2, language="eng", codec="ac3"),
        ],
        subtitle_tracks=[
            SubtitleTrack(index=0, stream_index=3, language="spa", codec="subrip"),
        ],
    )

    callback = AsyncMock()
    view = StreamTrackSelectView(info, callback)

    assert view.selected_audio_track == 0
    assert view.selected_subtitle_track == -1
    # Check that Audio and Subtitle Select items are added
    select_types = [type(item) for item in view.children]
    assert AudioTrackSelect in select_types
    assert SubtitleTrackSelect in select_types


@pytest.mark.asyncio
async def test_view_creation_single_audio_no_subs():
    info = MediaTracksInfo(
        url="http://example.com/stream.m3u8",
        audio_tracks=[
            AudioTrack(index=0, stream_index=1, language="spa", codec="aac"),
        ],
        subtitle_tracks=[],
    )

    callback = AsyncMock()
    view = StreamTrackSelectView(info, callback)

    select_types = [type(item) for item in view.children]
    assert AudioTrackSelect not in select_types
    assert SubtitleTrackSelect not in select_types
