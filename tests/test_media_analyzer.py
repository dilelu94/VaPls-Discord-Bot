"""Tests for media_analyzer.py and user taste memory storage in geminiCommand.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import media_analyzer
import geminiCommand


def test_is_analyzable_url():
    assert media_analyzer.is_analyzable_url("https://youtube.com/watch?v=123") is True
    assert media_analyzer.is_analyzable_url("https://example.com/article") is True
    assert media_analyzer.is_analyzable_url("https://tenor.com/view/cat-gif-123") is False
    assert media_analyzer.is_analyzable_url("https://cdn.discordapp.com/emojis/123.png") is False
    assert media_analyzer.is_analyzable_url("") is False


@pytest.mark.asyncio
async def test_get_video_duration():
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"12.5\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        duration = await media_analyzer.get_video_duration("https://example.com/video.mp4")
        assert duration == 12.5


@pytest.mark.asyncio
async def test_extract_video_middle_frame():
    mock_duration_proc = AsyncMock()
    mock_duration_proc.returncode = 0
    mock_duration_proc.communicate.return_value = (b"20.0\n", b"")

    fake_jpeg = b"\xff\xd8\xff\xe0fakejpegbytes"
    mock_ffmpeg_proc = AsyncMock()
    mock_ffmpeg_proc.returncode = 0
    mock_ffmpeg_proc.communicate.return_value = (fake_jpeg, b"")

    def subprocess_side_effect(*args, **kwargs):
        if args[0] == "ffprobe":
            return mock_duration_proc
        return mock_ffmpeg_proc

    with patch("asyncio.create_subprocess_exec", side_effect=subprocess_side_effect):
        frame = await media_analyzer.extract_video_middle_frame("https://example.com/video.mp4")
        assert frame == fake_jpeg


@pytest.mark.asyncio
async def test_analyze_content_interest():
    mock_reply = MagicMock()
    mock_reply.text = "A Seba le interesan los videos de autos antiguos"

    with patch("geminiClient.generate", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = mock_reply
        result = await media_analyzer.analyze_content_interest(
            "Seba", "video", image_bytes=b"\xff\xd8\xfffake"
        )
        assert result == "A Seba le interesan los videos de autos antiguos."
        assert mock_gen.called


@pytest.mark.asyncio
async def test_record_user_interest_and_format_long_term():
    guild_id = 999111222
    target_user = "Caro"
    statement = "A Caro le atraen las noticias de inteligencia artificial."

    # Clear memory for test guild
    lt_key = f"guild-{guild_id}"
    geminiCommand._indio_long_term.pop(lt_key, None)

    # Record interest
    ok = await geminiCommand.record_user_interest(guild_id, target_user, statement)
    assert ok is True

    # Verify memory content
    lt_data = geminiCommand._indio_long_term.get(lt_key, {})
    user_data = lt_data.get("users", {}).get("Caro", {})
    assert statement in user_data.get("gustos", [])

    # Verify natural language formatting in system prompt
    formatted = geminiCommand._format_long_term(lt_data)
    assert "Caro" in formatted
    assert "gustos e intereses:" in formatted
    assert statement in formatted
