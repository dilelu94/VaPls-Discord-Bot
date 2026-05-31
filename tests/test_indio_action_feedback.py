"""Behavior: when a tool the indio promised ("dale, va") fails after the fact,
a corrective message lands in the channel so the user doesn't end up waiting
forever on music that's never coming.

This pins the failure-feedback layer (``_failure_feedback`` +
``_post_action_failures``) and the end-to-end behavior through
``_dispatch_indio_actions``. Failures get translated into Spanish apologies;
successes don't trigger any apology spam.
"""
from __future__ import annotations

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


# --- End-to-end: _dispatch_indio_actions posts feedback on failure --------


async def test_dispatch_posts_apology_when_resume_finds_no_player(
        monkeypatch):
    """The Tobi case from the logs: bot just restarted, ``resume_music``
    finds no player, the indio already said "dale, va". The dispatcher must
    post a corrective message in the conversation channel."""
    import geminiCommand
    import playCommand

    # No player for guild 100 — simulates the post-restart state.
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)
    # Stub the userbot relay so we control whether feedback goes through it.
    monkeypatch.setattr(geminiCommand, "_relay_to_userbot",
                        AsyncMock(return_value=False))

    channel = MagicMock()
    channel.send = AsyncMock()

    bot = MagicMock()
    await geminiCommand._dispatch_indio_actions(
        bot, 100, [("RESUME_MUSIC", None)],
        feedback_channel_id=42,
        feedback_channel=channel,
    )

    # A corrective message was posted via the channel (relay returned False).
    assert channel.send.await_count >= 1
    posted = " ".join(str(c.args[0]) for c in channel.send.await_args_list)
    assert "no" in posted.lower()       # apology, not a fresh promise


async def test_dispatch_quiet_when_action_succeeds(monkeypatch):
    """A successful resume doesn't produce a "perdón" message — there's
    nothing to apologize for. Otherwise every play would get spammed with
    feedback noise."""
    import geminiCommand
    import playCommand
    from types import SimpleNamespace

    vc = MagicMock()
    vc.is_paused.return_value = True
    vc.is_playing.return_value = False

    player = SimpleNamespace(
        vc=vc, currentSong={"id": "x", "title": "t"},
        interrupted=False,
        togglePausePlay=AsyncMock(),
    )
    monkeypatch.setattr(playCommand, "guildPlayers", {100: player}, raising=True)
    monkeypatch.setattr(geminiCommand, "_relay_to_userbot",
                        AsyncMock(return_value=True))

    channel = MagicMock()
    channel.send = AsyncMock()

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        feedback_channel_id=42,
        feedback_channel=channel,
    )

    # The action succeeded → no apology sent through the fallback channel.
    # (The relay was called for the success status indicator — that's the
    # normal action mirror, not a failure message.)
    channel.send.assert_not_awaited()


async def test_dispatch_dedupes_identical_failure_messages(monkeypatch):
    """If two actions happen to produce the same feedback message (e.g. two
    failed controls on a vanished player) the user shouldn't see the same
    apology twice in a row."""
    import geminiCommand
    import playCommand

    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)
    monkeypatch.setattr(geminiCommand, "_relay_to_userbot",
                        AsyncMock(return_value=False))

    channel = MagicMock()
    channel.send = AsyncMock()

    await geminiCommand._dispatch_indio_actions(
        MagicMock(), 100,
        [("SKIP_MUSIC", None), ("PAUSE_MUSIC", None), ("RESUME_MUSIC", None)],
        feedback_channel_id=42,
        feedback_channel=channel,
    )

    # All three actions emit "no active player" — collapse to a single apology.
    assert channel.send.await_count == 1
