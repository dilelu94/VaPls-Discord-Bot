"""Behavior: ordinal-word selections ("la primera", "ponela tres") only fire
when the ordinal is preceded by a selection-intent word. Bare ordinals
("uno", "dos") embedded in normal speech must NOT register as a vote.

Anchor case from the 2026-05-31 logs: the line "Uno lava todo, la de
Jalimpita." landed inside an open vote and got parsed as "vote for option 1"
(because the token "uno" matched the ordinal stem), which silently played
the wrong track. The fix requires "la"/"ponela"/etc. immediately before the
ordinal token.
"""
from __future__ import annotations


_CANDIDATES = [
    {"id": "id1", "title": "Tema A"},
    {"id": "id2", "title": "Tema B"},
    {"id": "id3", "title": "Tema C"},
]


def test_bare_ordinal_in_normal_speech_does_not_vote():
    """The bug from the logs — "Uno" inside ordinary speech is NOT a vote."""
    from geminiCommand import _parse_choice
    assert _parse_choice("Uno lava todo, la de Jalimpita.", _CANDIDATES) is None


def test_bare_dos_does_not_vote():
    """Same idea with "dos" — must not autotrip the second option."""
    from geminiCommand import _parse_choice
    assert _parse_choice("Cobramos dos pesos por café", _CANDIDATES) is None


def test_la_plus_ordinal_still_votes():
    """The most common natural way to select — "la primera" / "la dos" — has
    to keep working. "la" before the ordinal IS valid selection context."""
    from geminiCommand import _parse_choice
    assert _parse_choice("ponme la primera", _CANDIDATES) == 0
    assert _parse_choice("la dos", _CANDIDATES) == 1


def test_imperative_plus_ordinal_votes():
    """Imperative verbs like "ponela" / "elegí" / "dale" are valid context."""
    from geminiCommand import _parse_choice
    assert _parse_choice("ponela tres", _CANDIDATES) == 2
    assert _parse_choice("elegí primera", _CANDIDATES) == 0


def test_digit_always_votes_regardless_of_context():
    """Explicit digits ("la 4", "ponela 2") were never the false-positive
    source — they continue to work without selection context."""
    from geminiCommand import _parse_choice
    assert _parse_choice("3", _CANDIDATES) == 2
    assert _parse_choice("Indio, tiradela 2", _CANDIDATES) == 1


def test_cancel_words_still_recognized():
    """Cancel logic is independent of the ordinal-context rule."""
    from geminiCommand import _parse_choice
    assert _parse_choice("ninguna", _CANDIDATES) == "cancel"
    assert _parse_choice("dejalo así", _CANDIDATES) == "cancel"
