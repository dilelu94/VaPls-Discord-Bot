"""Behavior: the indio's prompt carries a live snapshot of the music player
state so Gemini can disambiguate "play" / "pone play" / "continuá":
- Paused → the prompt explicitly steers ambiguous play requests to
  ``resume_music`` (not ``play_music`` with a junk query).
- Playing → the prompt mentions what's sonando so chat references work.
- No player / idle → no block injected (zero overhead, no false signal).

Tests are scoped to the pure helper (`_format_player_state`) because every
prompt the indio assembles flows through it, and the helper is the seam
where playback truth meets prompt-building.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _vc(*, paused=False, playing=False):
    vc = MagicMock(name="VoiceClient")
    vc.is_paused = MagicMock(return_value=paused)
    vc.is_playing = MagicMock(return_value=playing)
    return vc


def _player(*, current_title=None, paused=False, playing=False):
    return SimpleNamespace(
        vc=_vc(paused=paused, playing=playing),
        currentSong=({"id": "x", "title": current_title} if current_title else None),
    )


@pytest.fixture(autouse=True)
def _isolate_players(monkeypatch):
    """Each test owns its own guildPlayers dict so they don't bleed."""
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)
    yield


def test_no_guild_returns_empty():
    import geminiCommand
    assert geminiCommand._format_player_state(MagicMock(), None) == ""


def test_no_active_player_returns_empty():
    import geminiCommand
    # no entry registered for guild 42
    assert geminiCommand._format_player_state(MagicMock(), 42) == ""


def test_player_without_vc_returns_empty():
    import geminiCommand
    import playCommand
    playCommand.guildPlayers[42] = SimpleNamespace(vc=None, currentSong=None)
    assert geminiCommand._format_player_state(MagicMock(), 42) == ""


def test_paused_state_includes_disambiguation_rule():
    """The whole point of this block — when paused, the prompt tells Gemini
    that ambiguous "play"-like requests should resolve to resume_music."""
    import geminiCommand
    import playCommand
    playCommand.guildPlayers[42] = _player(
        current_title="Queen - Another One Bites The Dust",
        paused=True,
    )
    out = geminiCommand._format_player_state(MagicMock(), 42)
    assert out, "expected a non-empty player state block"
    low = out.lower()
    # Pinned facts: it must say paused, mention the title, and steer to
    # resume_music — not assert exact wording.
    assert "pausa" in low
    assert "queen - another one bites the dust" in low.lower()
    assert "resume_music" in low
    assert "play_music" in low  # exclusion clause


def test_paused_state_without_title_still_steers():
    """Even when the current title is lost (no currentSong), the disambiguation
    rule still has to fire so Gemini does not default to a junk play_music."""
    import geminiCommand
    import playCommand
    playCommand.guildPlayers[42] = _player(paused=True)
    out = geminiCommand._format_player_state(MagicMock(), 42)
    assert out
    assert "resume_music" in out.lower()


def test_playing_state_mentions_title_without_disambiguation():
    """While playing, the indio just needs context about what's on — no
    disambiguation rule, because there's nothing paused to confuse with."""
    import geminiCommand
    import playCommand
    playCommand.guildPlayers[42] = _player(
        current_title="Bizarrap - Session #58", playing=True,
    )
    out = geminiCommand._format_player_state(MagicMock(), 42)
    assert "Bizarrap - Session #58" in out
    assert "resume_music" not in out.lower()


def test_fully_idle_player_returns_empty():
    """Player exists, vc exists, but nothing is playing or paused → no block.
    Skipping the line keeps the prompt smaller and avoids confusing signals."""
    import geminiCommand
    import playCommand
    playCommand.guildPlayers[42] = _player()
    assert geminiCommand._format_player_state(MagicMock(), 42) == ""
