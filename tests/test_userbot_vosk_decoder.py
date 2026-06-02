"""Behavior: the VOSK wake-word check accepts a match from ANY of the N-best
alternatives the decoder emits, not just the top-1. This is what gives us
recall for speakers where VOSK ranks the bare token ("indio") above the
correct phrase ("indio dale"). Also covers backwards-compat with the legacy
single-best result format.

Tests `_vosk_heard_wake_word` and `_build_vosk_grammar` extracted from
``userbot/bot.py`` so we don't need to import the discord-side code.

The helper returns ``(matched, result_dict)`` — tests look only at the
first element since the second is forwarded raw to the dispatch payload
and any change in shape would not affect wake-word semantics.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest


_USERBOT_BOT = Path(__file__).resolve().parent.parent / "userbot" / "bot.py"


def _extract_decoder_helpers():
    """Pull the helpers needed to exercise the decoder logic. Same idea as
    the matcher test's extractor — we exec just the function defs we need
    into a clean namespace so the discord setup doesn't run.
    """
    src = _USERBOT_BOT.read_text()
    # Stop each extraction at the next top-level def/class so we never drag
    # in code that imports voice_recv or other discord-only modules.
    _TERM = r"(?=^(?:def |class |[A-Z_]+ ?= ?))"
    blocks = []
    for name, pattern in [
        ("_normalize", r"^def _normalize\(.*?" + _TERM),
        # Preset constants + sensitivity globals — needed by _build_vosk_grammar
        # and _active_wake_patterns which are called at module exec time.
        (
            "_preset_constants",
            r"^_PRESET_1_PATTERNS:.*?^_vosk_grammar_generation: int = 0\n",
        ),
        ("_WAKE_ANTI_PATTERNS", r"^_WAKE_ANTI_PATTERNS:[^\n]*\n"),
        (
            "_build_vosk_grammar",
            r"^def _build_vosk_grammar\(.*?^_VOSK_GRAMMAR = _build_vosk_grammar\(\)\n",
        ),
        # _WAKE_PATTERNS = _PRESET_1_PATTERNS (one line after _VOSK_GRAMMAR)
        ("_WAKE_PATTERNS", r"^_WAKE_PATTERNS:[^\n]*\n"),
        ("_set_sensitivity", r"^def _set_sensitivity\(.*?" + _TERM),
        ("_active_wake_patterns", r"^def _active_wake_patterns\(.*?" + _TERM),
        ("_text_matches_wake_pattern", r"^def _text_matches_wake_pattern\(.*?" + _TERM),
        ("_text_has_anti_pattern", r"^def _text_has_anti_pattern\(.*?" + _TERM),
        ("_vosk_heard_wake_word", r"^def _vosk_heard_wake_word\(.*?" + _TERM),
    ]:
        m = re.search(pattern, src, re.MULTILINE | re.DOTALL)
        assert m, f"could not locate {name}"
        blocks.append(m.group(0))

    import unicodedata as _unicodedata

    ns = {
        "unicodedata": _unicodedata,
        "json": json,
        "log": logging.getLogger("test_vosk_decoder"),
    }
    exec("\n".join(blocks), ns)
    return ns


_NS = _extract_decoder_helpers()
heard = _NS["_vosk_heard_wake_word"]
build_grammar = _NS["_build_vosk_grammar"]
VOSK_GRAMMAR = _NS["_VOSK_GRAMMAR"]
set_sensitivity = _NS["_set_sensitivity"]


class _FakeRec:
    """Stand-in for KaldiRecognizer that returns whatever JSON we pass in."""

    def __init__(self, result_payload: dict):
        self._payload = json.dumps(result_payload)

    def Result(self) -> str:
        return self._payload


# ---- _vosk_heard_wake_word: gating on accepted ---------------------------


def test_not_accepted_returns_false_without_reading_state():
    rec = _FakeRec({"alternatives": [{"text": "indio dale"}]})
    assert heard(rec, accepted=False)[0] is False


# ---- _vosk_heard_wake_word: N-best (alternatives format) -----------------


def test_alternatives_top1_matches_fires():
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "indio dale", "confidence": -10.0},
                {"text": "indio", "confidence": -11.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is True


def test_alternatives_second_matches_fires():
    # Top-1 is bare "indio" (no pattern). Alt #2 is "indio dale" → match.
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "indio", "confidence": -10.0},
                {"text": "indio dale", "confidence": -11.5},
                {"text": "indio por", "confidence": -12.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is True


def test_alternatives_none_match_returns_false():
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "indio", "confidence": -10.0},
                {"text": "indio anda", "confidence": -11.0},
                {"text": "indio mucho", "confidence": -12.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is False


def test_alternatives_empty_list_returns_false():
    rec = _FakeRec({"alternatives": []})
    assert heard(rec, accepted=True)[0] is False


def test_alternatives_che_indio_in_top1_fires():
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "che indio", "confidence": -8.0},
                {"text": "que indio", "confidence": -9.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is True


# ---- anti-pattern: "el indio" should NOT fire ---------------------------


def test_el_indio_top1_does_not_fire():
    """Speaker said "el indio" (third-person mention). Even with N-best,
    we must NOT gatillar el wake-word."""
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "el indio", "confidence": -8.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is False


def test_el_indio_as_alternative_vetoes_match():
    """VOSK top-1 says "che indio" but alt #2 says "el indio" — that's the
    signal the audio is ambiguous between "che" and "el". Veto the match
    to avoid the common false-positive."""
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "che indio", "confidence": -8.0},
                {"text": "el indio", "confidence": -8.5},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is False


def test_el_indio_with_accent_is_vetoed():
    """`_normalize` strips diacritics, so "él indio" reduces to ("el","indio")
    and hits the anti-pattern."""
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "él indio", "confidence": -8.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is False


def test_che_indio_without_el_indio_still_fires():
    """Sanity: if N-best does NOT include "el indio", the wake-word fires."""
    rec = _FakeRec(
        {
            "alternatives": [
                {"text": "che indio", "confidence": -8.0},
                {"text": "que indio", "confidence": -9.0},
                {"text": "indio dale", "confidence": -10.0},
            ]
        }
    )
    assert heard(rec, accepted=True)[0] is True


# ---- _vosk_heard_wake_word: legacy single-best format --------------------


def test_legacy_text_format_matches_fires():
    """Without SetMaxAlternatives, VOSK emits {"text": "..."} only."""
    rec = _FakeRec({"text": "indio tirate"})
    assert heard(rec, accepted=True)[0] is True


def test_legacy_text_format_no_match_returns_false():
    rec = _FakeRec({"text": "indio anda"})
    assert heard(rec, accepted=True)[0] is False


def test_empty_result_returns_false():
    rec = _FakeRec({})
    assert heard(rec, accepted=True)[0] is False


def test_malformed_result_does_not_crash():
    """If Result() raises or returns garbage, the helper swallows it."""

    class Boom:
        def Result(self):
            raise RuntimeError("decoder exploded")

    assert heard(Boom(), accepted=True)[0] is False


# ---- _build_vosk_grammar: contents ---------------------------------------


def test_grammar_is_valid_json_list():
    tokens = json.loads(VOSK_GRAMMAR)
    assert isinstance(tokens, list)
    assert len(tokens) > 0


def test_grammar_contains_canonical_wake_phrases():
    tokens = json.loads(VOSK_GRAMMAR)
    for phrase in (
        "che indio",
        "indio dale",
        "indio tirate",
        "indio ponete",
        "indio reproduci",
    ):
        assert phrase in tokens, f"grammar missing {phrase!r}"


def test_grammar_contains_relaxed_collapses():
    """Tokens that VOSK-small produces when it mishears the canonical form.

    The verb collapses ("indio por", "indio tira") exist in every preset; the
    "que indio"/"eh indio" invocation variants are preset-1 only (preset 2, the
    default, drops them), so they are checked against the preset-1 grammar.
    """
    tokens = json.loads(VOSK_GRAMMAR)
    for phrase in ("indio por", "indio tira"):
        assert phrase in tokens, f"grammar missing {phrase!r}"
    try:
        set_sensitivity(1)
        p1_tokens = json.loads(build_grammar())
    finally:
        set_sensitivity(2)
    for phrase in ("que indio", "eh indio"):
        assert phrase in p1_tokens, f"preset-1 grammar missing {phrase!r}"


def test_grammar_has_unk_catchall():
    tokens = json.loads(VOSK_GRAMMAR)
    assert "[unk]" in tokens


def test_grammar_dropped_unrelated_filler():
    """The shrunk grammar must not contain filler that isn't part of any
    wake-word pair. These tokens used to bloat the LM."""
    tokens = json.loads(VOSK_GRAMMAR)
    for dropped in ("boludo", "loco", "posta", "vamos", "viste", "ahre"):
        assert dropped not in tokens, f"grammar should not contain {dropped!r}"
