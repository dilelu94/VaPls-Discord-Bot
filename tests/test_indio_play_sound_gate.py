"""Behavior: el Indio solo reproduce un clip del soundpad cuando alguien lo
PIDE (verbo de orden: tirá/pone/reproducí/…) o cuando NOMBRA textualmente un
clip que existe. Si la charla no tiene ninguna de las dos cosas, el modelo
puede equivocarse y disparar play_sound por libre asociación — eso se descarta
y queda solo la respuesta de texto.

Esto fija el gate determinístico (`_gate_play_sound_actions`) que corre sobre el
mensaje crudo del usuario, sin sumar otra llamada a Gemini. El caso real que
motivó el cambio: a la pregunta "¿de qué cuadro soy?" el Indio tiró el clip de
los Simpsons "los niños pueden ser muy crueles" sin que nadie lo pidiera.
"""
from __future__ import annotations

import pytest

from geminiCommand import _gate_play_sound_actions


def _sounds(actions):
    """Solo los PLAY_SOUND que sobrevivieron al gate."""
    return [arg for act, arg in actions if act == "PLAY_SOUND"]


# --- Caso que motivó el cambio: ni verbo ni nombre → no suena --------------

def test_misfire_without_order_or_name_is_dropped():
    """El incidente de Tobi: pregunta de charla, el modelo igual eligió un
    clip que no se nombró. El audio se descarta."""
    actions = [("PLAY_SOUND", "los niños pueden ser muy crueles")]
    kept = _gate_play_sound_actions(actions, "¿Qué indio? ¿De qué cuadro soy?")
    assert _sounds(kept) == []


def test_dropping_sound_keeps_other_actions_path():
    """Descartar el clip NO toca el resto: el gate solo filtra PLAY_SOUND."""
    actions = [("PLAY_SOUND", "milapollo")]
    kept = _gate_play_sound_actions(actions, "hoy comí milanesa con papas")
    assert _sounds(kept) == []


# --- Caso A: hay verbo de orden → suena -----------------------------------

@pytest.mark.parametrize("msg", [
    "tirá el pezpija",
    "pone el de las risas",
    "metele milapollo",
    "hacé sonar el de aplausos",
    "reproducí el del relincho",
    "dale, tirate ese audio",
])
def test_explicit_order_verb_lets_the_clip_play(msg):
    """Con un imperativo de reproducción el clip suena, aunque el nombre que
    eligió el modelo no esté calcado en el mensaje."""
    actions = [("PLAY_SOUND", "lo que sea que el modelo eligio")]
    kept = _gate_play_sound_actions(actions, msg)
    assert len(_sounds(kept)) == 1


# --- Caso B: nombran el clip sin pedirlo → suena como extra ----------------

def test_named_clip_without_order_still_plays():
    """Si dicen textualmente el nombre del clip (sin verbo de orden) el audio
    sale igual, como yapa."""
    actions = [("PLAY_SOUND", "risas")]
    kept = _gate_play_sound_actions(actions, "jaja el audio de las risas es lo más")
    assert _sounds(kept) == ["risas"]


def test_named_clip_match_ignores_accents_and_case():
    """El match normaliza tildes y mayúsculas."""
    actions = [("PLAY_SOUND", "relincho")]
    kept = _gate_play_sound_actions(actions, "Me MUERO con el RELINCHO ese")
    assert len(_sounds(kept)) == 1


def test_generic_words_in_name_do_not_ground_it():
    """Un nombre compuesto solo por palabras genéricas + una keyword no debe
    'anclar' por las genéricas: si la keyword no está, no suena."""
    actions = [("PLAY_SOUND", "el de la risa")]
    kept = _gate_play_sound_actions(actions, "no sé de qué hablás la verdad")
    assert _sounds(kept) == []


# --- El gate no toca otras acciones ---------------------------------------

def test_non_sound_actions_pass_through_untouched():
    """play_music / controles no se filtran nunca, con o sin verbo."""
    actions = [("PLAY_MUSIC", "soda stereo"), ("SKIP_MUSIC", "")]
    kept = _gate_play_sound_actions(actions, "pasá al que sigue")
    assert kept == actions


def test_order_verb_lets_sound_through_alongside_music():
    """Mezcla: una acción de música + un sonido comandado conviven."""
    actions = [("PLAY_MUSIC", "queen"), ("PLAY_SOUND", "aplausos")]
    kept = _gate_play_sound_actions(actions, "pone queen y metele el de aplausos")
    assert ("PLAY_MUSIC", "queen") in kept
    assert _sounds(kept) == ["aplausos"]


def test_empty_actions_returns_empty():
    assert _gate_play_sound_actions([], "lo que sea") == []
