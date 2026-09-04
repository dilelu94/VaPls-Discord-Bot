import pytest
from media_inspector import (
    AudioTrack,
    SubtitleTrack,
    MediaTracksInfo,
    _parse_ffprobe_json,
)

SAMPLE_FFPROBE_DATA = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "channels": 2,
            "tags": {"language": "spa", "title": "Español Latino"},
        },
        {
            "index": 2,
            "codec_name": "ac3",
            "codec_type": "audio",
            "channels": 6,
            "tags": {"language": "eng", "title": "English 5.1"},
        },
        {
            "index": 3,
            "codec_name": "subrip",
            "codec_type": "subtitle",
            "disposition": {"forced": 0},
            "tags": {"language": "spa", "title": "Español Completo"},
        },
        {
            "index": 4,
            "codec_name": "ass",
            "codec_type": "subtitle",
            "disposition": {"forced": 1},
            "tags": {"language": "eng", "title": "English Forced"},
        },
    ]
}


def test_parse_ffprobe_json_extracts_tracks():
    info = _parse_ffprobe_json(SAMPLE_FFPROBE_DATA, "http://example.com/movie.mkv")
    assert info.has_multiple_audios is True
    assert info.has_subtitles is True
    assert len(info.audio_tracks) == 2
    assert len(info.subtitle_tracks) == 2

    a0 = info.audio_tracks[0]
    assert a0.index == 0
    assert a0.stream_index == 1
    assert a0.language == "spa"
    assert a0.channels == 2
    assert "Español" in a0.display_name

    a1 = info.audio_tracks[1]
    assert a1.index == 1
    assert a1.stream_index == 2
    assert a1.language == "eng"
    assert a1.channels == 6

    s0 = info.subtitle_tracks[0]
    assert s0.index == 0
    assert s0.stream_index == 3
    assert s0.language == "spa"
    assert s0.is_forced is False

    s1 = info.subtitle_tracks[1]
    assert s1.index == 1
    assert s1.stream_index == 4
    assert s1.is_forced is True
    assert "(Forzados)" in s1.display_name


def test_single_audio_no_subs():
    data = {
        "streams": [
            {"index": 0, "codec_type": "video"},
            {"index": 1, "codec_type": "audio", "channels": 2},
        ]
    }
    info = _parse_ffprobe_json(data, "http://example.com/stream.m3u8")
    assert info.has_multiple_audios is False
    assert info.has_subtitles is False
