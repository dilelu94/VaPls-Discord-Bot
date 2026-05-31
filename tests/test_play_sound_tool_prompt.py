"""Behavior: the ``play_sound`` tool description tells Gemini in no uncertain
terms when NOT to call it — namely, when there's no explicit imperative verb
or no concrete clip keyword. Same shape as ``play_music`` but scoped to
soundpad clips.

Anchor case from the 2026-05-31 logs: Miles asked "indio tenés el pez que
pescó chalo?" (pure conversation question, no command verb) and the indio
fired play_sound with name="PezPija". Gemini fuzzy-matched "el pez" against
the soundpad and ignored that the message was a question, because the old
description ("usala cuando te piden un audio") had no hard verb requirement.

These tests pin the wording so accidental rewrites that loosen the gate get
caught. They don't assert exact phrases — only that the key signal words
(verb list, hard requirement framing, invalid-example) are present.
"""
from __future__ import annotations


def _play_sound_description():
    from geminiCommand import _INDIO_TOOLS
    for tool in _INDIO_TOOLS:
        if tool.get("name") == "play_sound":
            return tool["description"].lower()
    raise AssertionError("play_sound tool missing from _INDIO_TOOLS")


def test_lists_the_required_imperative_verbs():
    """The prompt must enumerate the verbs that ARE valid triggers so Gemini
    has a positive list to anchor on. Without this, it fuzzy-matches any
    mention of a clip keyword as a play_sound request."""
    desc = _play_sound_description()
    for verb in ("tirá", "tirate", "pone", "metele", "hacé sonar"):
        assert verb in desc, f"play_sound prompt missing verb {verb!r}"


def test_says_both_verb_and_subject_are_required():
    """The hard requirement: a verb AND a clip name. The prompt must say both
    are needed, otherwise Gemini fires on bare mentions of any soundpad
    keyword that surfaces in conversation."""
    desc = _play_sound_description()
    assert "verbo" in desc or "imperativ" in desc
    # The "concrete clip name" framing — wording varies but must be present.
    assert "nombre" in desc or "keyword" in desc


def test_calls_out_a_negative_conversation_example():
    """Pin at least one INVALID example showing that a question or chat
    mention of a clip keyword is NOT a play_sound trigger. The 'pez' /
    'chalo' anchor reflects the 2026-05-31 production failure."""
    desc = _play_sound_description()
    # The witness case or the broader 'pregunta de charla, no pedido' framing.
    assert "pez" in desc or "charla" in desc or "pregunta" in desc


def test_warns_against_fuzzy_matching_conversation():
    """The key insight: Gemini was matching ANY soundpad keyword that showed
    up in conversation as a play_sound trigger. The prompt must call this
    out explicitly so the model doesn't keep doing it."""
    desc = _play_sound_description()
    # Either explicit "no significa que quieran" wording or general
    # "hablando del tema" framing.
    assert "no significa" in desc or "hablando del tema" in desc
