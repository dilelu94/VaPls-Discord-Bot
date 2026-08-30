"""Behavior: ask_indio_stream_vision sends a stream snapshot JPEG + context to Gemini,
returning Indio's commentary string or None if Gemini returns 'SKIP' or fails."""
import pytest
import geminiCommand


async def test_stream_vision_returns_commentary_on_valid_reply(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "Mirá esa peteada tremenda que te hiciste ahí en la C!"}]},
        }],
    })
    res = await geminiCommand.ask_indio_stream_vision(
        image_bytes=b"fake_jpeg_bytes",
        streamer_name="Miles",
    )
    assert res == "Mirá esa peteada tremenda que te hiciste ahí en la C!"


async def test_stream_vision_returns_none_on_skip(gemini_http):
    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "SKIP"}]},
        }],
    })
    res = await geminiCommand.ask_indio_stream_vision(
        image_bytes=b"fake_jpeg_bytes",
        streamer_name="Miles",
    )
    assert res is None


async def test_stream_vision_returns_none_on_empty_bytes():
    res = await geminiCommand.ask_indio_stream_vision(
        image_bytes=b"",
        streamer_name="Miles",
    )
    assert res is None
