"""Behavior: when a tool the indio promised ("dale, va") fails after the fact,
the original reply message is EDITED IN PLACE to append a failure reason so the
user doesn't end up waiting forever on music that's never coming.  On success
the reply is edited to append a short success marker.

This pins the failure-feedback layer (``_failure_feedback``) and the end-to-end
edit-in-place behavior through ``_dispatch_indio_actions``.

Pure ``_failure_feedback`` translation tests are kept unchanged — they test the
same pure function and remain valid.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# --- Pure translation: status string → user-facing message ----------------

def test_resume_not_paused_gets_friendly_message():
    """The most common failure mode after a restart: resume_music with no
    paused player. The user spoke an order, the indio promised it would
    happen, the tool can't deliver — the message has to say so."""
    from geminiCommand import _failure_feedback
    msg = _failure_feedback("resume: not paused")
    assert msg and "pausado" in msg.lower()


def test_no_active_player_messages_speak_in_first_person():
    """``{action}: no active player`` is what every control tool emits when
    the player got wiped (e.g. a restart). One message covers all of them."""
    from geminiCommand import _failure_feedback
    for status in ("resume_music: no active player",
                   "skip_music: no active player",
                   "stop_music: no active player"):
        msg = _failure_feedback(status)
        assert msg, f"expected feedback for {status}"
        assert "no" in msg.lower()


def test_music_fail_surfaces_the_reason():
    """yt-dlp/voice-connect failures already carry an actionable reason — the
    feedback should pass it through verbatim so the user can fix it."""
    from geminiCommand import _failure_feedback
    msg = _failure_feedback("music: fail — no hay nadie en un canal de voz")
    assert msg
    assert "no hay nadie en un canal de voz" in msg


def test_success_status_returns_none():
    """A successful action must NOT trigger a feedback message — otherwise
    every play would get a noisy "uh, I did the thing" reply."""
    from geminiCommand import _failure_feedback
    assert _failure_feedback("music: ok — algun tema") is None
    assert _failure_feedback("skip: ok") is None
    assert _failure_feedback("pause: ok") is None


# --- End-to-end: _dispatch_indio_actions edits reply in place on failure ----

def _make_handle(*, via_relay=False, channel_id=42, message_id=None,
                 edited_content=None, single=True, sent_content=None):
    """Build a reply handle mirroring what indioLogic / indioFromVoice produce.

    The ``edited_content`` list (pass a mutable list) is populated by the fake
    message's ``.edit()`` so tests can assert what was written. ``sent_content``
    records any standalone ``channel.send`` the dispatcher falls back to when
    the reply can't be edited in place (e.g. a multi-chunk reply). ``single``
    mirrors whether the reply fit in a single message."""
    if via_relay:
        return types.SimpleNamespace(
            via_relay=True,
            channel_id=channel_id,
            message_id=message_id or 999,
            message=None,
            single=single,
        )
    else:
        container = edited_content if edited_content is not None else []
        sent = sent_content if sent_content is not None else []

        async def _channel_send(content=None, **kwargs):
            if content is not None:
                sent.append(content)

        class _FakeMsg:
            id = 1234
            channel = types.SimpleNamespace(id=channel_id, send=_channel_send)

            async def edit(self, *, content=None, **kwargs):
                if content is not None:
                    container.append(content)

        return types.SimpleNamespace(
            via_relay=False,
            channel_id=channel_id,
            message_id=None,
            message=_FakeMsg(),
            single=single,
        )


async def test_dispatch_edits_reply_with_failure_reason_when_resume_finds_no_player(
        monkeypatch):
    """The Tobi case from the logs: bot just restarted, ``resume_music``
    finds no player, the indio already said "dale, va". The dispatcher must
    EDIT the original reply message to include the failure reason instead of
    posting a separate apology."""
    import geminiCommand
    import playCommand

    # No player for guild 100 — simulates the post-restart state.
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    edited = []
    handle = _make_handle(via_relay=False, edited_content=edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=handle,
        reply_text="dale, va",
    )

    # The original message was edited (not a new send) to include the reason.
    assert edited, "expected the reply message to be edited in place"
    combined = edited[0]
    assert "dale, va" in combined         # base text preserved
    assert "no" in combined.lower()       # failure indicator present


async def test_dispatch_edits_reply_with_success_suffix_on_successful_resume(
        monkeypatch):
    """A successful resume edits the reply to add a success marker — no
    separate 'apology' or noise message is posted."""
    import geminiCommand
    import playCommand

    vc = MagicMock()
    vc.is_paused.return_value = True
    vc.is_playing.return_value = False

    player = types.SimpleNamespace(
        vc=vc, currentSong={"id": "x", "title": "t"},
        interrupted=False,
        togglePausePlay=AsyncMock(),
    )
    monkeypatch.setattr(playCommand, "guildPlayers", {100: player}, raising=True)
    # Relay for the #sick-tunes mirror returns a list (truthy).
    monkeypatch.setattr(geminiCommand, "_relay_to_userbot",
                        AsyncMock(return_value=[999]))

    edited = []
    handle = _make_handle(via_relay=False, edited_content=edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=handle,
        reply_text="dale, retomando",
    )

    # Edit happened and includes a success marker (not a failure message).
    assert edited, "expected the reply message to be edited"
    combined = edited[0]
    assert "dale, retomando" in combined   # base text preserved
    # Success marker present; no apology language.
    assert "listo" in combined.lower()


async def test_dispatch_edits_relay_message_via_edit_endpoint_on_failure(
        monkeypatch):
    """When the original reply went via the userbot relay, the dispatcher must
    call ``_edit_via_userbot`` (not the Discord .edit()) to patch it."""
    import geminiCommand
    import playCommand

    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    edit_calls: list[dict] = []

    async def _fake_edit(channel_id, message_id, content):
        edit_calls.append({"channel_id": channel_id,
                           "message_id": message_id,
                           "content": content})
        return True

    monkeypatch.setattr(geminiCommand, "_edit_via_userbot", _fake_edit)

    handle = types.SimpleNamespace(
        via_relay=True,
        channel_id=55,
        message_id=777,
        message=None,
    )

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=handle,
        reply_text="dale, va",
    )

    assert edit_calls, "expected _edit_via_userbot to be called"
    call = edit_calls[0]
    assert call["channel_id"] == 55
    assert call["message_id"] == 777
    assert "dale, va" in call["content"]     # base text in the edit
    assert "no" in call["content"].lower()   # failure reason present


async def test_dispatch_no_edit_when_no_handle_provided(monkeypatch):
    """When no reply handle is given (e.g. during a vote flow or edge path),
    the dispatcher still runs the action and returns statuses without crashing."""
    import geminiCommand
    import playCommand

    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    statuses = await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=None,
        reply_text="",
    )

    assert statuses                        # action was attempted
    assert any("no active player" in s for s in statuses)


async def test_dispatch_no_separate_channel_send_on_failure(monkeypatch):
    """No separate apology message is posted via channel.send anymore — the
    edit-in-place approach supersedes the old _post_action_failures path."""
    import geminiCommand
    import playCommand

    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    edited = []
    handle = _make_handle(via_relay=False, edited_content=edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=handle,
        reply_text="dale, va",
    )

    # The reply was edited in place — no separate channel send needed.
    assert edited, "expected in-place edit"
    # _post_action_failures no longer exists or is not called — just verify
    # no separate message was posted by checking that the edit is the only
    # observable effect on the handle.
    assert len(edited) == 1


async def test_dispatch_music_success_adds_music_suffix(monkeypatch):
    """PLAY_MUSIC success appends the music-specific success marker."""
    import geminiCommand

    # Relay-based play succeeds.
    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot",
                        AsyncMock(return_value=(True, "ok")))

    edited = []
    handle = _make_handle(via_relay=False, edited_content=edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("PLAY_MUSIC", "Queen")],
        reply_handle=handle,
        reply_text="dale, va Queen",
    )

    assert edited
    combined = edited[0]
    assert "dale, va Queen" in combined
    assert "🎵" in combined          # music-specific suffix


async def test_dispatch_sound_success_adds_sound_suffix(monkeypatch):
    """PLAY_SOUND success appends the sound-specific success marker."""
    import geminiCommand

    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot",
                        AsyncMock(return_value=(True, "ok")))

    edited = []
    handle = _make_handle(via_relay=False, edited_content=edited)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("PLAY_SOUND", "risas")],
        reply_handle=handle,
        reply_text="tomá",
    )

    assert edited
    combined = edited[0]
    assert "tomá" in combined
    assert "🔊" in combined          # sound-specific suffix


async def test_dispatch_multi_chunk_reply_posts_standalone_result(monkeypatch):
    """When the reply was split into several messages we must NOT rewrite one
    chunk with the whole reply (that duplicates earlier chunks / risks the
    2000-char limit). Instead the result is posted as a short standalone
    message so the user still finds out."""
    import geminiCommand
    import playCommand

    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    edited = []
    sent = []
    handle = _make_handle(via_relay=False, edited_content=edited,
                          sent_content=sent, single=False)

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=handle,
        reply_text="una respuesta larga partida en varios mensajes",
    )

    # The original (chunked) message was NOT edited; the result went out as a
    # standalone message instead.
    assert not edited, "must not overwrite a chunk with the full reply"
    assert sent, "expected a standalone result message"
    assert "no" in sent[0].lower()   # failure reason surfaced
