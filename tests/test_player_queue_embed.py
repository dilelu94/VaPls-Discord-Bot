"""Behavior: /queue renders the current player state into a single embed.

These tests pin the contract that a user invoking /queue sees:

- When nothing is active, the bot says so plainly.
- When music is playing, the embed shows what's playing AND what's coming.
- A long queue is capped and the user is told how many more remain.
- A total time is computed from item durations, tolerating empty / "NA" cells.
- The history count surfaces alongside, mirroring the persistent control message.

`build_queue_embed` is a pure function — we exercise it directly with a real
`GuildPlayer` (no Discord context needed). Assertions look at the rendered
embed text, not at exact wording or call counts, so the copy can evolve.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _embed_text(embed) -> str:
    """All visible text in the embed, joined — for substring assertions."""
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def _field(embed, *needles: str):
    """Return the field whose name contains every needle (case-insensitive)."""
    for field in embed.fields:
        name = (field.name or "").lower()
        if all(n.lower() in name for n in needles):
            return field
    return None


def _make_player(*, current=None, queue=None, history=None):
    """Build a GuildPlayer with just the state `build_queue_embed` reads."""
    import playCommand
    player = playCommand.GuildPlayer(100, MagicMock())
    player.currentSong = current
    player.queue = list(queue or [])
    player.history = list(history or [])
    return player


def _song(vid: str, title: str, duration: str = ""):
    return {"id": vid, "title": title, "duration_string": duration}


# --------------------------------------------------------------------------
# Empty / inactive states
# --------------------------------------------------------------------------

def test_no_player_signals_nothing_active():
    """If no GuildPlayer exists for this guild, /queue must say so — not crash."""
    import playCommand
    embed = playCommand.build_queue_embed(None)

    text = _embed_text(embed).lower()
    assert "no hay música" in text or "nada" in text or "vac" in text, (
        f"empty-state embed should tell the user nothing is active; got: {text!r}"
    )
    # And it should NOT mention a song or queue count.
    assert "▶️" not in text and "siguientes" not in text.lower()


def test_player_with_nothing_playing_and_empty_queue_signals_nothing_active():
    """Same outcome whether the player is missing or just idle."""
    player = _make_player(current=None, queue=[], history=[])
    import playCommand
    embed = playCommand.build_queue_embed(player)

    text = _embed_text(embed).lower()
    assert "no hay música" in text or "nada" in text or "vac" in text


# --------------------------------------------------------------------------
# Active states
# --------------------------------------------------------------------------

def test_shows_current_song_and_short_queue():
    """User sees what's playing now AND every item in a short queue."""
    player = _make_player(
        current=_song("v0", "Now Playing Track", "3:42"),
        queue=[
            _song("v1", "Next Song", "2:30"),
            _song("v2", "After That", "4:00"),
            _song("v3", "Then This One", "1:15"),
        ],
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    text = _embed_text(embed)
    # Current song surfaced with its duration.
    assert "Now Playing Track" in text
    assert "3:42" in text
    # All three queued titles present.
    assert "Next Song" in text
    assert "After That" in text
    assert "Then This One" in text
    # Queue length is communicated somewhere.
    assert "3" in text


def test_long_queue_is_capped_and_remainder_count_shown():
    """A 20-song queue must NOT render 20 lines — cap is enforced and the
    remainder is communicated so the user knows there's more."""
    queue = [_song(f"v{i}", f"Track {i}", "3:00") for i in range(1, 21)]
    player = _make_player(
        current=_song("v0", "Current", "2:00"),
        queue=queue,
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    text = _embed_text(embed)
    # First few tracks are listed.
    assert "Track 1" in text
    assert "Track 15" in text
    # The 16th track is NOT individually listed.
    assert "Track 16" not in text
    assert "Track 20" not in text
    # But the user is told there are more, with the right remainder count.
    assert "5" in text  # 20 - 15 = 5 remaining
    # And the total queue size is communicated.
    assert "20" in text


def test_empty_queue_with_current_song_says_queue_empty():
    """Only a current song, nothing queued — user should see 'cola vacía' or
    equivalent, not absent fields that look like a bug."""
    player = _make_player(
        current=_song("v0", "Only Track", "2:00"),
        queue=[],
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    text = _embed_text(embed).lower()
    assert "only track" in text.lower() or "Only Track" in _embed_text(embed)
    # The queue section should communicate emptiness.
    queue_field = _field(embed, "cola") or _field(embed, "siguientes")
    assert queue_field is not None
    assert "vac" in (queue_field.value or "").lower()


# --------------------------------------------------------------------------
# Duration totals
# --------------------------------------------------------------------------

def test_total_time_sums_queue_durations_in_mmss_form():
    """Durations like '3:00' and '1:30' sum to '4:30' in the total field."""
    player = _make_player(
        current=_song("v0", "Current", "5:00"),
        queue=[
            _song("v1", "A", "3:00"),
            _song("v2", "B", "1:30"),
        ],
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    total_field = _field(embed, "tiempo") or _field(embed, "total")
    assert total_field is not None, "expected a total-time field when durations exist"
    assert "4:30" in total_field.value


def test_total_time_ignores_empty_durations():
    """When yt-dlp returns '' for a track (live streams, NA), it must not
    poison the sum — the other tracks still contribute correctly."""
    player = _make_player(
        current=_song("v0", "Current", "5:00"),
        queue=[
            _song("v1", "A", "3:00"),
            _song("v2", "B", ""),
            _song("v3", "C", "1:30"),
        ],
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    total_field = _field(embed, "tiempo") or _field(embed, "total")
    assert total_field is not None
    assert "4:30" in total_field.value


def test_total_time_renders_hours_when_queue_is_long():
    """Above an hour, the total formats as H:MM:SS — confirms the formatter
    swap so the user reads '1:30:00', not '90:00'."""
    player = _make_player(
        current=_song("v0", "Current"),
        queue=[_song(f"v{i}", f"T{i}", "30:00") for i in range(3)],  # 3 × 30min = 1h30
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    total_field = _field(embed, "tiempo") or _field(embed, "total")
    assert total_field is not None
    assert "1:30:00" in total_field.value


def test_no_total_field_when_all_durations_missing():
    """If we have no useful duration data, don't render a misleading 0:00."""
    player = _make_player(
        current=_song("v0", "Current"),
        queue=[_song("v1", "A"), _song("v2", "B")],  # all duration_string=""
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    total_field = _field(embed, "tiempo") or _field(embed, "total")
    assert total_field is None


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def test_history_count_surfaces_when_history_is_nonempty():
    """The user should see how many tracks have already played, consistent
    with what the persistent control message shows."""
    player = _make_player(
        current=_song("v0", "Current", "2:00"),
        queue=[_song("v1", "A", "1:00")],
        history=[_song(f"h{i}", f"Past {i}") for i in range(7)],
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    text = _embed_text(embed)
    assert "7" in text  # exact count visible somewhere


def test_no_history_field_when_history_is_empty():
    """Don't render 'Canciones en historial: 0' noise on a fresh player."""
    player = _make_player(
        current=_song("v0", "Current", "2:00"),
        queue=[_song("v1", "A", "1:00")],
        history=[],
    )
    import playCommand
    embed = playCommand.build_queue_embed(player)

    # The "history" wording should not leak into the embed at all.
    text = _embed_text(embed).lower()
    assert "historial" not in text
