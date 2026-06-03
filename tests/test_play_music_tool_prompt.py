"""Behavior: the ``play_music`` tool description tells Gemini in no uncertain
terms when NOT to call it — namely, when there's no explicit imperative verb
("ponete", "metele", "tirá", "reproduci") or no concrete subject (artist,
song, genre).

Anchor case from the 2026-05-31 logs: someone said "Sacá esta música." and
the indio fired play_music with a junk query (started GRUPO INDIO EXITAZOS).
The verb "sacá" means stop, not play — but Gemini ignored that because the
old description didn't list the play-verbs as a hard requirement.

These tests pin the wording so accidental rewrites that remove the explicit
verb list get caught. They don't assert exact phrases — only that the prompt
contains the key signal words.
"""

from __future__ import annotations


def _play_music_description():
    from geminiCommand import _INDIO_TOOLS

    for tool in _INDIO_TOOLS:
        if tool.get("name") == "play_music":
            return tool["description"].lower()
    raise AssertionError("play_music tool missing from _INDIO_TOOLS")


def test_lists_the_required_imperative_verbs():
    """The prompt must enumerate the verbs that ARE valid triggers so Gemini
    has a positive list to anchor on. Without this, it tends to fire on any
    mention of music."""
    desc = _play_music_description()
    for verb in ("poné", "ponete", "reproducí"):
        assert verb in desc, f"play_music prompt missing verb {verb!r}"


def test_says_both_verb_and_subject_are_required():
    """The hard requirement: a verb AND a subject. The prompt must say both
    are needed, otherwise Gemini fires on bare verbs or bare mentions."""
    desc = _play_music_description()
    # Phrasing varies but the rule has to be there.
    assert "verbo" in desc or "imperativ" in desc
    # Subject framing: artist/song/genre.
    assert "artista" in desc or "canción" in desc or "cancion" in desc


def test_calls_out_the_sacá_case_or_similar():
    """Pin at least one INVALID example so future edits don't accidentally
    drop the negative-examples list."""
    desc = _play_music_description()
    # Either the literal "sacá" counterexample or "stop_music" routing hint.
    assert "saca" in desc or "stop_music" in desc


def test_mentions_resume_music_disambiguation():
    """Existing rule: bare "play" / "dale play" without a subject is
    resume_music, not play_music. Keep this in the description."""
    desc = _play_music_description()
    assert "resume_music" in desc


def test_play_music_has_no_dale_mention():
    """'Dale' ya no aparece en la descripción: mencionarlo, incluso como
    contraejemplo, mantiene el concepto activo y genera falsos positivos
    (Gemini termina interpretando 'dale' como verbo de orden)."""
    desc = _play_music_description()
    assert "dale" not in desc
