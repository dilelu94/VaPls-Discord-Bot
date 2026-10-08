"""Behavioral and unit tests for transcriptBugTracker module.

Pins observable behavior:
- Detection of direct replies to audio transcript messages.
- Detection of replies to Indio persona messages associated with audio transcripts.
- Creation of structured GitHub Issues via githubIssues.create_issue.
- Ingestion of explicit Grok debugging instructions in the Issue body.
- Deduplication and commenting on existing issues.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

import transcriptBugTracker


def test_is_audio_transcript_message():
    # Message starting with transcript emoji & header
    msg_emoji = types.SimpleNamespace(content="🎙️ **Miles:** ¡Dobre indiado!", attachments=[])
    assert transcriptBugTracker.is_audio_transcript_message(msg_emoji) is True

    # Message with audio_escuchado attachment
    att = types.SimpleNamespace(filename="audio_escuchado_12345.wav", content_type="audio/wav", url="https://cdn.discord.com/audio.wav")
    msg_att = types.SimpleNamespace(content="Some audio", attachments=[att])
    assert transcriptBugTracker.is_audio_transcript_message(msg_att) is True

    # Regular text message
    msg_plain = types.SimpleNamespace(content="Hola a todos", attachments=[])
    assert transcriptBugTracker.is_audio_transcript_message(msg_plain) is False

    # None message
    assert transcriptBugTracker.is_audio_transcript_message(None) is False


def test_parse_transcript_text():
    speaker, text = transcriptBugTracker.parse_transcript_text("🎙️ **Miles:** ¡Dobre indiado!")
    assert speaker == "Miles"
    assert text == "¡Dobre indiado!"

    speaker2, text2 = transcriptBugTracker.parse_transcript_text("🎙️ Hola indio")
    assert speaker2 == "Desconocido"
    assert text2 == "Hola indio"


def test_format_issue_body_contains_required_sections():
    body = transcriptBugTracker.format_issue_body(
        user_reply_text="En realidad dije 'Buenas tardes'",
        erroneous_transcription="¡Dobre indiado!",
        indio_response="¿Cómo andás, chango?",
        audio_filename="audio_escuchado_123.wav",
        audio_url="https://cdn.discordapp.com/attachments/123/audio.wav",
        speaker="Miles",
        reporter_name="Seba",
        reporter_id=999,
        guild_name="Server Test",
        channel_name="general",
        transcript_message_id=111,
        reply_message_id=222,
    )

    assert "Buenas tardes" in body
    assert "¡Dobre indiado!" in body
    assert "¿Cómo andás, chango?" in body
    assert "audio_escuchado_123.wav" in body
    assert "https://cdn.discordapp.com/attachments/123/audio.wav" in body
    assert "Grok" in body
    assert "transcript-111" in body


@pytest.mark.asyncio
async def test_check_and_report_transcript_bug_direct_reply():
    # Direct reply to transcript message
    att = types.SimpleNamespace(filename="audio_escuchado_123.wav", content_type="audio/wav", url="https://cdn.discord.com/audio.wav")
    transcript_msg = types.SimpleNamespace(
        id=1001,
        content="🎙️ **Miles:** ¡Dobre indiado!",
        attachments=[att],
        reference=None,
    )

    reply_msg = types.SimpleNamespace(
        id=2002,
        content="Dije 'Buenas tardes'",
        author=types.SimpleNamespace(display_name="Miles", id=555, bot=False),
        guild=types.SimpleNamespace(name="Servidor Test"),
        channel=types.SimpleNamespace(name="general", fetch_message=AsyncMock(return_value=transcript_msg)),
        reference=types.SimpleNamespace(message_id=1001),
        referenced_message=transcript_msg,
        add_reaction=AsyncMock(),
    )

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value=None)), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=42)) as mock_create:
        issue_no = await transcriptBugTracker.check_and_report_transcript_bug(reply_msg)

        assert issue_no == 42
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert "[Auto-Bug]" in call_kwargs["title"]
        assert "Dije 'Buenas tardes'" in call_kwargs["body"]
        assert "¡Dobre indiado!" in call_kwargs["body"]
        assert "autobug" in call_kwargs["labels"]
        assert "stt-error" in call_kwargs["labels"]
        reply_msg.add_reaction.assert_called_once_with("🐛")


@pytest.mark.asyncio
async def test_check_and_report_transcript_bug_reply_to_indio_response():
    # User replies to Indio message, whose parent is the transcript audio message
    att = types.SimpleNamespace(filename="audio_escuchado_123.wav", content_type="audio/wav", url="https://cdn.discord.com/audio.wav")
    transcript_msg = types.SimpleNamespace(
        id=1001,
        content="🎙️ **Miles:** ¡Dobre indiado!",
        attachments=[att],
        reference=None,
    )

    indio_msg = types.SimpleNamespace(
        id=1500,
        content="¿Cómo andás, chango? ¿Todo piola?",
        author=types.SimpleNamespace(display_name="Indio", id=777, bot=True),
        reference=types.SimpleNamespace(message_id=1001),
        referenced_message=transcript_msg,
    )

    reply_msg = types.SimpleNamespace(
        id=2002,
        content="Lo que dije fue 'Hola Indio'",
        author=types.SimpleNamespace(display_name="Seba", id=888, bot=False),
        guild=types.SimpleNamespace(name="Servidor Test"),
        channel=types.SimpleNamespace(name="general", fetch_message=AsyncMock()),
        reference=types.SimpleNamespace(message_id=1500),
        referenced_message=indio_msg,
        add_reaction=AsyncMock(),
    )

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value=None)), \
         patch("githubIssues.create_issue", new=AsyncMock(return_value=99)) as mock_create:
        issue_no = await transcriptBugTracker.check_and_report_transcript_bug(reply_msg)

        assert issue_no == 99
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert "Lo que dije fue 'Hola Indio'" in call_kwargs["body"]
        assert "¿Cómo andás, chango? ¿Todo piola?" in call_kwargs["body"]
        assert "¡Dobre indiado!" in call_kwargs["body"]


@pytest.mark.asyncio
async def test_check_and_report_transcript_bug_existing_issue_comment():
    # Deduplication: issue already exists, add comment instead of creating
    att = types.SimpleNamespace(filename="audio_escuchado_123.wav", content_type="audio/wav", url="https://cdn.discord.com/audio.wav")
    transcript_msg = types.SimpleNamespace(
        id=1001,
        content="🎙️ **Miles:** ¡Dobre indiado!",
        attachments=[att],
        reference=None,
    )

    reply_msg = types.SimpleNamespace(
        id=2002,
        content="Dije 'Buenas tardes'",
        author=types.SimpleNamespace(display_name="Miles", id=555, bot=False),
        guild=types.SimpleNamespace(name="Servidor Test"),
        channel=types.SimpleNamespace(name="general"),
        reference=types.SimpleNamespace(message_id=1001),
        referenced_message=transcript_msg,
        add_reaction=AsyncMock(),
    )

    with patch("githubIssues.find_issue_by_fingerprint", new=AsyncMock(return_value={"number": 42})), \
         patch("githubIssues.add_comment", new=AsyncMock(return_value=True)) as mock_comment, \
         patch("githubIssues.create_issue", new=AsyncMock()) as mock_create:
        issue_no = await transcriptBugTracker.check_and_report_transcript_bug(reply_msg)

        assert issue_no == 42
        mock_create.assert_not_called()
        mock_comment.assert_called_once()
        reply_msg.add_reaction.assert_called_once_with("🐛")


@pytest.mark.asyncio
async def test_ignore_non_reply_messages():
    plain_msg = types.SimpleNamespace(
        id=3003,
        content="Un mensaje cualquiera",
        author=types.SimpleNamespace(display_name="User", bot=False),
        reference=None,
    )

    with patch("githubIssues.create_issue", new=AsyncMock()) as mock_create:
        res = await transcriptBugTracker.check_and_report_transcript_bug(plain_msg)
        assert res is None
        mock_create.assert_not_called()
