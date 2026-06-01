"""Behavior: the long-term memory pipeline relies on pure helpers — extracting
JSON from a model reply, clamping it to safe bounds, rendering it back into a
Spanish prompt block, and flattening turns to text for compression. These are
deterministic, so we test them directly."""
import geminiCommand as gc
from geminiCommand import (
    _clamp_long_term,
    _extract_json,
    _format_long_term,
    _turns_to_text,
)


# ---- _extract_json -------------------------------------------------------
def test_extract_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_markdown_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_prose():
    assert _extract_json('claro, aquí va: {"a": 1} listo') == {"a": 1}


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("no hay json acá") is None
    assert _extract_json("") is None


def test_extract_json_returns_none_for_non_object():
    assert _extract_json("[1, 2, 3]") is None


# ---- _clamp_long_term ----------------------------------------------------
def test_clamp_enforces_structure_on_garbage():
    out = _clamp_long_term("not a dict")
    assert out == {"users": {}, "eventos_del_grupo": [], "chistes_internos": []}


def test_clamp_caps_traits_per_user():
    raw = {"users": {"Mati": {"traits": [f"t{i}" for i in range(20)]}}}
    out = _clamp_long_term(raw)
    assert len(out["users"]["Mati"]["traits"]) == gc._LT_TRAITS_PER_USER


def test_clamp_caps_events_and_jokes():
    raw = {
        "eventos_del_grupo": [f"e{i}" for i in range(50)],
        "chistes_internos": [f"j{i}" for i in range(50)],
    }
    out = _clamp_long_term(raw)
    assert len(out["eventos_del_grupo"]) == gc._LT_GROUP_EVENTS
    assert len(out["chistes_internos"]) == gc._LT_JOKES


def test_clamp_truncates_long_strings():
    raw = {"eventos_del_grupo": ["x" * 500]}
    out = _clamp_long_term(raw)
    assert len(out["eventos_del_grupo"][0]) <= 120


def test_clamp_excludes_indio_as_user():
    raw = {"users": {"indio": {"traits": ["soy el bot"]},
                     "Mati": {"traits": ["fan de python"]}}}
    out = _clamp_long_term(raw)
    assert "indio" not in out["users"]
    assert "Mati" in out["users"]


# ---- _format_long_term ---------------------------------------------------
def test_format_with_empty_gemini_input_does_not_crash():
    """Empty Gemini long-term doesn't crash and returns a string.

    Behavior shifted with the static lore feature (users.py GROUP_LORE +
    per-user dossiers): empty Gemini data still renders the manual baseline
    so the indio always has context. We only pin that the function tolerates
    empty input and does not invent a current_members header on its own.
    """
    out = _format_long_term({})
    assert isinstance(out, str)
    assert "Mis amigos son:" not in out


def test_format_renders_users_events_and_jokes():
    lt = {
        "users": {"Mati": {"traits": ["fan de python"],
                           "preguntas_tipicas": ["cómo deployar"],
                           "anecdotas": ["rompió prod un viernes"]}},
        "eventos_del_grupo": ["maratón de tortas"],
        "chistes_internos": ["el del pingüino"],
    }
    rendered = _format_long_term(lt)
    assert "Mati" in rendered
    assert "fan de python" in rendered
    assert "maratón de tortas" in rendered
    assert "el del pingüino" in rendered


# ---- _turns_to_text ------------------------------------------------------
def test_turns_to_text_labels_speakers_and_skips_empty():
    turns = [
        {"role": "user", "parts": [{"text": "Mati: hola"}]},
        {"role": "model", "parts": [{"text": "qué onda"}]},
        {"role": "user", "parts": [{"text": ""}]},     # skipped
    ]
    out = _turns_to_text(turns)
    assert "grupo: Mati: hola" in out
    assert "indio: qué onda" in out
    assert out.count("\n") == 1                          # only two non-empty lines
