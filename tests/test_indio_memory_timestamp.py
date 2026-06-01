"""Behavior: when the indio replays old history into its next prompt, turns
older than ~15 min get a ``[hace X]`` tag so Gemini can tell "this happened in
a past conversation" apart from "this is what we just said".

Without this, the indio confused last week's "te pasé la lista" with the
current exchange and told a user it had given them options that never existed.

The tag is added at prompt-assembly time (not in storage) so old persisted
entries without a ``ts`` field still benefit, and the storage format stays
simple."""
from __future__ import annotations

import time


def _turn(role, text, ts=None):
    t = {"role": role, "parts": [{"text": text}]}
    if ts is not None:
        t["ts"] = ts
    return t


def test_recent_turn_passes_through_unchanged():
    """A turn from a minute ago is part of the current exchange — no tag."""
    from geminiCommand import _stamp_history_for_prompt
    now = time.time()
    history = [_turn("user", "che indio cómo va", ts=now - 60)]
    out = _stamp_history_for_prompt(history, now)
    assert out[0]["parts"][0]["text"] == "che indio cómo va"
    # ts must NOT survive into the prompt payload — Gemini doesn't know about it.
    assert "ts" not in out[0]


def test_old_turn_gets_a_temporal_tag():
    """A turn from 2 days ago must be prefixed with a clear "[hace X]" cue,
    so the model treats it as past, not present."""
    from geminiCommand import _stamp_history_for_prompt
    now = time.time()
    history = [_turn("user", "pasame la lista de redondos", ts=now - 86400 * 2)]
    out = _stamp_history_for_prompt(history, now)
    text = out[0]["parts"][0]["text"]
    assert text.startswith("[hace ")
    assert text.endswith("pasame la lista de redondos")


def test_legacy_turn_without_ts_is_tagged_as_old():
    """Persisted history from before this feature has no ``ts`` field.
    Treat those as old rather than as current — better safe than confusing
    the model with possibly-ancient context."""
    from geminiCommand import _stamp_history_for_prompt
    out = _stamp_history_for_prompt(
        [_turn("user", "vieja conversación")], time.time(),
    )
    text = out[0]["parts"][0]["text"]
    assert text.startswith("[hace ")


def test_mixed_recent_and_old_get_distinguished():
    """Realistic case: a long history with old turns + a fresh exchange.
    The fresh ones stay clean, the old ones get tagged. That's the whole
    point — Gemini sees the seam between past and present."""
    from geminiCommand import _stamp_history_for_prompt
    now = time.time()
    history = [
        _turn("user",  "qué onda?",          ts=now - 86400 * 5),
        _turn("model", "todo piola",         ts=now - 86400 * 5),
        _turn("user",  "che indio hola",     ts=now - 30),
        _turn("model", "qué onda capo",      ts=now - 25),
    ]
    out = _stamp_history_for_prompt(history, now)
    assert out[0]["parts"][0]["text"].startswith("[hace ")
    assert out[1]["parts"][0]["text"].startswith("[hace ")
    # The fresh ones are untouched.
    assert out[2]["parts"][0]["text"] == "che indio hola"
    assert out[3]["parts"][0]["text"] == "qué onda capo"


def test_stamping_does_not_mutate_input():
    """The helper must return a copy — callers re-use ``history_snapshot``
    elsewhere (e.g. for storage assembly). Mutating it would corrupt that."""
    from geminiCommand import _stamp_history_for_prompt
    now = time.time()
    original = [_turn("user", "hola", ts=now - 86400)]
    _ = _stamp_history_for_prompt(original, now)
    assert original[0]["parts"][0]["text"] == "hola"
    assert original[0]["ts"] == now - 86400
