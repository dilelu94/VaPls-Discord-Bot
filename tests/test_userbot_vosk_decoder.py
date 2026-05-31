"""Behavior: the VOSK wake-word check accepts a match from ANY of the N-best
alternatives the decoder emits, not just the top-1. This is what gives us
recall for speakers where VOSK ranks the bare token ("indio") above the
correct phrase ("indio dale"). Also covers backwards-compat with the legacy
single-best result format.

Tests `_vosk_heard_wake_word` and `_build_vosk_grammar` extracted from
``userbot/bot.py`` so we don't need to import the discord-side code.
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
        ("_normalize",
         r"^def _normalize\(.*?" + _TERM),
        ("_WAKE_PATTERNS",
         r"^_WAKE_PATTERNS:.*?^\)\n"),
        ("_text_matches_wake_pattern",
         r"^def _text_matches_wake_pattern\(.*?" + _TERM),
        ("_build_vosk_grammar",
         r"^def _build_vosk_grammar\(.*?^_VOSK_GRAMMAR = _build_vosk_grammar\(\)\n"),
        ("_vosk_heard_wake_word",
         r"^def _vosk_heard_wake_word\(.*?" + _TERM),
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


class _FakeRec:
    """Stand-in for KaldiRecognizer that returns whatever JSON we pass in."""

    def __init__(self, result_payload: dict):
        self._payload = json.dumps(result_payload)

    def Result(self) -> str:
        return self._payload


# ---- _vosk_heard_wake_word: gating on accepted ---------------------------

def test_not_accepted_returns_false_without_reading_state():
    rec = _FakeRec({"alternatives": [{"text": "indio dale"}]})
    assert heard(rec, accepted=False) is False


# ---- _vosk_heard_wake_word: N-best (alternatives format) -----------------

def test_alternatives_top1_matches_fires():
    rec = _FakeRec({"alternatives": [
        {"text": "indio dale", "confidence": -10.0},
        {"text": "indio",       "confidence": -11.0},
    ]})
    assert heard(rec, accepted=True) is True


def test_alternatives_second_matches_fires():
    # Top-1 is bare "indio" (no pattern). Alt #2 is "indio dale" → match.
    rec = _FakeRec({"alternatives": [
        {"text": "indio",       "confidence": -10.0},
        {"text": "indio dale",  "confidence": -11.5},
        {"text": "indio por",   "confidence": -12.0},
    ]})
    assert heard(rec, accepted=True) is True


def test_alternatives_none_match_returns_false():
    rec = _FakeRec({"alternatives": [
        {"text": "indio",        "confidence": -10.0},
        {"text": "indio anda",   "confidence": -11.0},
        {"text": "indio mucho",  "confidence": -12.0},
    ]})
    assert heard(rec, accepted=True) is False


def test_alternatives_empty_list_returns_false():
    rec = _FakeRec({"alternatives": []})
    assert heard(rec, accepted=True) is False


def test_alternatives_che_indio_in_top1_fires():
    rec = _FakeRec({"alternatives": [
        {"text": "che indio", "confidence": -8.0},
        {"text": "que indio", "confidence": -9.0},
    ]})
    assert heard(rec, accepted=True) is True


# ---- _vosk_heard_wake_word: legacy single-best format --------------------

def test_legacy_text_format_matches_fires():
    """Without SetMaxAlternatives, VOSK emits {"text": "..."} only."""
    rec = _FakeRec({"text": "indio tirate"})
    assert heard(rec, accepted=True) is True


def test_legacy_text_format_no_match_returns_false():
    rec = _FakeRec({"text": "indio anda"})
    assert heard(rec, accepted=True) is False


def test_empty_result_returns_false():
    rec = _FakeRec({})
    assert heard(rec, accepted=True) is False


def test_malformed_result_does_not_crash():
    """If Result() raises or returns garbage, the helper swallows it."""
    class Boom:
        def Result(self):
            raise RuntimeError("decoder exploded")
    assert heard(Boom(), accepted=True) is False


# ---- _build_vosk_grammar: contents ---------------------------------------

def test_grammar_is_valid_json_list():
    tokens = json.loads(VOSK_GRAMMAR)
    assert isinstance(tokens, list)
    assert len(tokens) > 0


def test_grammar_contains_canonical_wake_phrases():
    tokens = json.loads(VOSK_GRAMMAR)
    for phrase in ("che indio", "indio dale", "indio tirate",
                   "indio ponete", "indio reproduci"):
        assert phrase in tokens, f"grammar missing {phrase!r}"


def test_grammar_contains_relaxed_collapses():
    """Tokens that VOSK-small produces when it mishears the canonical form."""
    tokens = json.loads(VOSK_GRAMMAR)
    for phrase in ("que indio", "eh indio", "indio por", "indio tira"):
        assert phrase in tokens, f"grammar missing {phrase!r}"


def test_grammar_has_unk_catchall():
    tokens = json.loads(VOSK_GRAMMAR)
    assert "[unk]" in tokens


def test_grammar_dropped_unrelated_filler():
    """The shrunk grammar must not contain filler that isn't part of any
    wake-word pair. These tokens used to bloat the LM."""
    tokens = json.loads(VOSK_GRAMMAR)
    for dropped in ("boludo", "loco", "posta", "vamos", "viste", "ahre"):
        assert dropped not in tokens, f"grammar should not contain {dropped!r}"
