"""Behavior: when the indio dispatches PLAY_MUSIC via the userbot relay
and the relay returns HTTP 200, that ack does *not* mean VaPls actually
queued or started playing the song — it only means Discord accepted the
slash invocation. yt-dlp may still 404, the channel may be empty, the
guild player may fail. So the indio's success suffix on the relay path
must NOT claim definitive completion ("listo ✅"); it should reflect that
the request was handed off, leaving the user to verify by ear.

When the local fallback (``playFromIndio``) succeeds the indio knows the
song is queued in-process and the regular "listo" suffix still applies.

Boundary mocked: ``_invoke_slash_via_userbot`` (the network call to the
userbot's HTTP endpoint) and ``playCommand.playFromIndio`` (the local
playback engine — we don't have FFmpeg or a real guild here). All other
dispatch logic runs for real.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock


def _make_handle(edited_list):
    """Reply handle whose ``.edit()`` appends to ``edited_list``. Mirrors
    the SimpleNamespace shape indioLogic produces for single-chunk replies.
    """

    async def _channel_send(content=None, **kwargs):
        pass

    class _FakeMsg:
        id = 1234
        channel = types.SimpleNamespace(id=42, send=_channel_send)

        async def edit(self, *, content=None, **kwargs):
            if content is not None:
                edited_list.append(content)

    return types.SimpleNamespace(
        via_relay=False,
        channel_id=42,
        message_id=None,
        message=_FakeMsg(),
        single=True,
    )


def _member_in_voice(user_id=42, channel_id=99, channel_name=None):
    """Requester stand-in that satisfies the music-action gating
    in ``_dispatch_indio_actions`` (has ``id`` + ``voice.channel``)."""
    return types.SimpleNamespace(
        id=user_id,
        voice=types.SimpleNamespace(
            channel=types.SimpleNamespace(id=channel_id, name=channel_name),
        ),
    )


async def test_play_music_via_relay_uses_softer_success_suffix(monkeypatch):
    """Relay ack-only path: the edit must reflect "I handed it off" rather
    than "I played it". The regular "listo" finality language is wrong
    here because the bot has no confirmation playback actually started."""
    import geminiCommand

    # Relay returns success — the only signal we have is the HTTP ack.
    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(True, "despacito")),
    )

    edited: list[str] = []
    handle = _make_handle(edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(),
        100,
        [("PLAY_MUSIC", "despacito")],
        reply_handle=handle,
        reply_text="dale, va",
        requester_member=_member_in_voice(),
    )

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "dale, va" in combined  # base text preserved
    # Observable promise: the message must NOT carry the strong-success
    # wording from the local-success path. "listo" reserved for cases
    # where the bot actually knows the action completed.
    assert "listo" not in combined.lower()


async def test_play_music_via_fallback_keeps_strong_success_suffix(monkeypatch):
    """Local fallback path: ``playFromIndio`` ran in-process and reports
    the song was queued. That's a real confirmation, so the regular
    "listo" finality language is appropriate."""
    import geminiCommand
    import playCommand

    # Relay fails so the dispatcher falls back to playFromIndio. That
    # function returns (ok=True, msg) confirming the song was queued.
    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(False, "relay error")),
    )
    monkeypatch.setattr(
        playCommand,
        "playFromIndio",
        AsyncMock(return_value=(True, "Despacito - Luis Fonsi")),
    )

    edited: list[str] = []
    handle = _make_handle(edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(),
        100,
        [("PLAY_MUSIC", "despacito")],
        reply_handle=handle,
        reply_text="dale, va",
        requester_member=_member_in_voice(),
    )

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "dale, va" in combined
    # In-process success is a real confirmation — the strong finality
    # wording is correct here.
    assert "listo" in combined.lower()


async def test_play_sound_via_relay_uses_softer_success_suffix(monkeypatch):
    """Same uncertainty applies to PLAY_SOUND via relay: HTTP 200 from the
    userbot only proves Discord accepted the slash, not that the clip
    actually played in the voice channel."""
    import geminiCommand

    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(True, "risa-de-tobi")),
    )

    edited: list[str] = []
    handle = _make_handle(edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(),
        100,
        [("PLAY_SOUND", "risa-de-tobi")],
        reply_handle=handle,
        reply_text="va eso",
    )

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "va eso" in combined
    assert "listo" not in combined.lower()


async def test_generate_image_via_relay_uses_softer_success_suffix(monkeypatch):
    import geminiCommand

    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(True, "un perrito")),
    )

    edited: list[str] = []
    handle = _make_handle(edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(),
        100,
        [("GENERATE_IMAGE", "un perrito")],
        reply_handle=handle,
        reply_text="ahí va la imagen",
    )

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "ahí va la imagen" in combined
    assert "le pasé el prompt al /generarimagen" in combined.lower()


async def test_generate_image_via_fallback(monkeypatch):
    import geminiCommand
    import huggingfaceImage
    import tempfile
    import os

    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, b"fake-png-data")
    finally:
        os.close(fd)

    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(False, "relay error")),
    )
    monkeypatch.setattr(
        huggingfaceImage,
        "generate",
        AsyncMock(return_value=path),
    )

    spy_unlink = MagicMock()
    monkeypatch.setattr(os, "unlink", spy_unlink)

    fake_chan = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = fake_chan
    ctx = MagicMock()

    edited: list[str] = []
    handle = _make_handle(edited)

    try:
        await geminiCommand._dispatch_indio_actions(
            mock_bot,
            100,
            [("GENERATE_IMAGE", "un perrito")],
            reply_handle=handle,
            reply_text="ahí va la imagen",
            requester_member=_member_in_voice(),
        )
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "ahí va la imagen" in combined
    assert "image: ok" in combined or "listo" in combined.lower()
    assert fake_chan.send.call_count == 1
    assert spy_unlink.call_count == 1


async def test_play_music_via_relay_includes_channel_mention(monkeypatch):
    """Relay path: the success suffix should include a clickable link
    to the designated play channel via Discord's ``<#id>`` mention format."""
    import geminiCommand
    import config

    monkeypatch.setattr(config, "INDIO_PLAY_CHANNEL_ID", 451607097432604672)
    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(True, "despacito")),
    )

    edited: list[str] = []
    handle = _make_handle(edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(),
        100,
        [("PLAY_MUSIC", "despacito")],
        reply_handle=handle,
        reply_text="dale, va",
        requester_member=_member_in_voice(),
        from_voice=True,
    )

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "<#451607097432604672>" in combined
    assert "🎵" in combined


async def test_play_music_via_fallback_includes_channel_mention(monkeypatch):
    """Fallback path: the success suffix should include a clickable link
    to the designated play channel via Discord's ``<#id>`` mention format."""
    import geminiCommand
    import config
    import playCommand

    monkeypatch.setattr(config, "INDIO_PLAY_CHANNEL_ID", 451607097432604672)
    monkeypatch.setattr(
        geminiCommand,
        "_invoke_slash_via_userbot",
        AsyncMock(return_value=(False, "relay error")),
    )
    monkeypatch.setattr(
        playCommand,
        "playFromIndio",
        AsyncMock(return_value=(True, "Despacito - Luis Fonsi")),
    )

    edited: list[str] = []
    handle = _make_handle(edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(),
        100,
        [("PLAY_MUSIC", "despacito")],
        reply_handle=handle,
        reply_text="dale, va",
        requester_member=_member_in_voice(),
        from_voice=True,
    )

    assert edited, "expected the reply to be edited with a result line"
    combined = edited[0]
    assert "<#451607097432604672>" in combined
    assert "🎵" in combined
