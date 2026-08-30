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


async def test_stream_vision_associates_user_memory_and_records_history(gemini_http, monkeypatch):
    monkeypatch.setattr(
        geminiCommand,
        "_USERS",
        {
            211354006805676032: {
                "name": "Miles",
                "traits": ["programador", "fan de Boca"],
                "anecdotas": ["tiene codornices"],
            }
        },
    )

    gemini_http(status=200, payload={
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "Che Miles te estás mandando cualquiera en el CS!"}]},
        }],
    })

    commentary = await geminiCommand.ask_indio_stream_vision(
        image_bytes=b"fake_jpeg_bytes",
        streamer_name="Miles",
        streamer_id=211354006805676032,
        guild_id=123456,
    )

    assert commentary == "Che Miles te estás mandando cualquiera en el CS!"
    history = geminiCommand._indio_history.get("guild-123456", [])
    assert len(history) >= 2
    assert "Miles" in history[-2]["parts"][0]["text"]
    assert commentary in history[-1]["parts"][0]["text"]

