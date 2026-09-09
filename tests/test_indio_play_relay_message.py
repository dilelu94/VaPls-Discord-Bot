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


async def test_play_music_via_relay_uses_no_robotic_success_suffix(monkeypatch):
    """Relay path: on success, the indio's reply should NOT be edited with robotic suffixes."""
    import geminiCommand

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

    assert not edited, "successful action should not append robotic suffixes"


async def test_play_music_via_fallback_uses_no_robotic_success_suffix(monkeypatch):
    """Local fallback path: on success, the indio's reply should NOT be edited with robotic suffixes."""
    import geminiCommand
    import playCommand

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

    assert not edited, "successful action should not append robotic suffixes"


async def test_play_sound_via_relay_uses_no_robotic_success_suffix(monkeypatch):
    """PLAY_SOUND via relay: on success, the indio's reply should NOT be edited with robotic suffixes."""
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

    assert not edited, "successful action should not append robotic suffixes"


async def test_play_music_via_relay_success_remains_clean(monkeypatch):
    """Relay path with voice context: on success, reply remains clean without robotic suffixes."""
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

    assert not edited, "successful action should not edit reply with robotic suffixes"


async def test_play_music_via_fallback_success_remains_clean(monkeypatch):
    """Fallback path with voice context: on success, reply remains clean without robotic suffixes."""
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

    assert not edited, "successful action should not edit reply with robotic suffixes"

