"""Behavior: /play skips the picker when the user's query clearly names the
song that came back as the top yt-dlp hit, and keeps showing the picker
otherwise. The whole point is to remove a friction click for specific queries
("el infierno encantador esta noche") while preserving the picker for vague
queries ("algo de rock") where the bot would otherwise gamble.

Tests assert on the autoplay/picker outcome — *not* on the threshold number,
the normalization strategy, or the helper internals — so the matching logic
can be swapped (token overlap, embeddings, etc.) without breaking the suite,
as long as these two anchor cases keep behaving the right way.
"""

from __future__ import annotations


def test_specific_query_autoplays_top_hit():
    """The classic case: user asks for a specific song and the top hit's
    title contains the same words (plus extras like artist, '(Audio Oficial)').
    Bot should queue directly, no picker."""
    import playCommand

    assert playCommand._should_autoplay_top(
        "el infierno esta encantado de esta noche",
        "Patricio Rey y sus Redonditos de Ricota - El Infierno esta Encantador esta Noche (Audio Oficial)",
    )


def test_short_query_autoplays_when_high_match():
    """Two-token queries can now autoplay when the top hit's title clearly
    contains the query as a substring (min-tokens lowered from 3 to 2)."""
    import playCommand

    assert playCommand._should_autoplay_top(
        "el infierno",
        "El infierno de Dante - audiolibro completo en español narrado",
    )


def test_unrelated_query_shows_picker():
    """3+ token query (passes the min-tokens guard) but no real overlap with
    the top hit's title — fuzzy ratio stays below the threshold so the picker
    still appears. This is the case where 'suave' shouldn't turn into
    'autoplay anything'."""
    import playCommand

    assert not playCommand._should_autoplay_top(
        "ponete jazz suave tranqui",
        "Top 100 Death Metal Headbangers Compilation",
    )


def test_accent_and_case_do_not_block_autoplay():
    """User types without accents, YouTube title has them. That's a
    normalization concern — the behavior must be: still autoplay."""
    import playCommand

    assert playCommand._should_autoplay_top(
        "cancion de alfonsina y el mar",
        "Canción de Alfonsina y el Mar - Mercedes Sosa",
    )
