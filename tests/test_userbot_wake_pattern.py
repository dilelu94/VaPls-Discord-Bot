"""Behavior: the VOSK wake-word matcher fires ONLY on the explicit phrases
"che indio", "indio ponete", "indio poneme", "indio reproducí" /
"indio reproduce". Bare "indio", "indio" with unrelated context, and short
utterances must NOT trigger — those were the historical sources of false
positives that saturated Whisper.

Also covers sensitivity presets: preset 2 drops "que indio"/"eh indio"
to reduce false positives from common Spanish words.

Tests the pure pattern functions (``_text_matches_wake_pattern``,
``_set_sensitivity``, ``_active_wake_patterns``) extracted from
``userbot/bot.py`` so we don't need a live VOSK model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_USERBOT_BOT = Path(__file__).resolve().parent.parent / "userbot" / "bot.py"


def _extract_wake_ns():
    """Pull the wake-word helpers and preset globals out of userbot/bot.py
    without executing the module's discord setup.

    bot.py builds a Discord client and monkey-patches voice_recv at import
    time. We can't import it in tests, so we exec just the function defs we
    need into a clean namespace.
    """
    src = _USERBOT_BOT.read_text()
    blocks = []

    # `_normalize`
    m = re.search(r"^def _normalize\(.*?(?=^\S|\Z)", src, re.MULTILINE | re.DOTALL)
    assert m, "could not locate _normalize"
    blocks.append(m.group(0))

    # Preset pattern constants, sensitivity globals, and _PRESET_3_FILLER.
    # Captures from _PRESET_1_PATTERNS through the closing bracket of
    # _PRESET_3_FILLER so that _build_vosk_grammar can reference it.
    m = re.search(
        r"^_PRESET_1_PATTERNS:.*?^_vosk_grammar_generation: int = 0\n",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate preset constants"
    blocks.append(m.group(0))

    # _PRESET_3_FILLER (placed after _vosk_grammar_generation)
    m3 = re.search(
        r"^_PRESET_3_FILLER:.*?^\]\n",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m3, "could not locate _PRESET_3_FILLER"
    blocks.append(m3.group(0))

    # `_build_vosk_grammar` (needed by _set_sensitivity)
    m = re.search(
        r"^def _build_vosk_grammar\(.*?(?=^def |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _build_vosk_grammar"
    blocks.append(m.group(0))

    # `_VOSK_GRAMMAR = _build_vosk_grammar()`
    m = re.search(r"^_VOSK_GRAMMAR = _build_vosk_grammar\(\)\n", src, re.MULTILINE)
    assert m, "could not locate _VOSK_GRAMMAR assignment"
    blocks.append(m.group(0))

    # `_WAKE_PATTERNS` constant
    m = re.search(r"^_WAKE_PATTERNS:.*\n", src, re.MULTILINE)
    assert m, "could not locate _WAKE_PATTERNS"
    blocks.append(m.group(0))

    # `_set_sensitivity`
    m = re.search(
        r"^def _set_sensitivity\(.*?(?=^def |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _set_sensitivity"
    blocks.append(m.group(0))

    # `_active_wake_patterns`
    m = re.search(
        r"^def _active_wake_patterns\(.*?(?=^def |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _active_wake_patterns"
    blocks.append(m.group(0))

    # `_text_matches_wake_pattern`
    m = re.search(
        r"^def _text_matches_wake_pattern\(.*?(?=^def |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _text_matches_wake_pattern"
    blocks.append(m.group(0))

    # `_whisper_confirms_indio`
    m = re.search(
        r"^def _whisper_confirms_indio\(.*?(?=^def |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _whisper_confirms_indio"
    blocks.append(m.group(0))

    import unicodedata as _unicodedata
    import logging as _logging
    import json as _json
    import threading as _threading
    from types import SimpleNamespace as _SimpleNamespace

    ns: dict = {
        "unicodedata": _unicodedata,
        "json": _json,
        # _set_sensitivity uses log.info; supply a no-op logger.
        "log": _logging.getLogger("test_wake_pattern"),
        "config": None,  # not used by the extracted functions
        "threading": _threading,
    }
    exec("\n".join(blocks), ns)
    return ns


_NS = _extract_wake_ns()
matches = _NS["_text_matches_wake_pattern"]
set_sensitivity = _NS["_set_sensitivity"]
active_patterns = _NS["_active_wake_patterns"]


# ---------------------------------------------------------------------------
# Preset 1 (default) — positive cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Wake-word invocation particles.
        "che indio",
        "Che indio",  # casing-insensitive
        "que indio",  # VOSK hears "che indio" as "que indio"
        "eh indio",  # other speakers come out as "eh indio"
        # Command-verb patterns.
        "indio ponete",
        "indio poneme",
        "indio reproduci",
        "indio reproducí",  # accent normalized via NFD
        "indio reproduce",
        "indio tirate",
        "indio dale",
        "indio dale play",  # one realistic combined-command sample
        # Collapsed-verb patterns.
        "indio por",  # VOSK collapses "ponete"/"poneme" to "por"
        "indio tira",  # VOSK drops trailing "te" → "tira"
    ],
)
def test_preset1_pattern_fires(text):
    set_sensitivity(1)
    assert matches(text) is True


# ---------------------------------------------------------------------------
# Preset 1 — negative cases (bare or unrelated context)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "indio",
        "el indio",
        "indio loco",  # historical false-positive: "indio + any word"
        "como andas",
        "che boludo",  # "che" alone, no "indio"
        "che indio ",  # trailing whitespace — still fires? actually yes
        "reproducí algo",  # missing "indio"
        "ponete a reproducir",  # missing "indio"
        "tirate de un puente",  # "tirate" alone without "indio" should not fire
        "dale que va",  # "dale" alone without "indio" should not fire
    ],
)
def test_preset1_does_not_fire_on_bare_or_unrelated(text):
    set_sensitivity(1)
    # The trailing-whitespace case is actually a valid hit; filter it out
    # of the negative assertion. Keep the entry to document the edge case.
    if text.strip() == "che indio":
        return
    assert matches(text) is False


# ---------------------------------------------------------------------------
# Preset 2 — "que indio" / "eh indio" no longer fire
# ---------------------------------------------------------------------------


def test_preset2_che_indio_fires():
    """Preset 2: 'che indio' still triggers the wake word."""
    set_sensitivity(2)
    assert matches("che indio") is True


@pytest.mark.parametrize("text", ["que indio", "eh indio"])
def test_preset2_que_eh_indio_do_not_fire(text):
    """Preset 2: 'que indio' and 'eh indio' are no longer active patterns."""
    set_sensitivity(2)
    assert matches(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "indio ponete",
        "indio poneme",
        "indio reproduci",
        "indio reproducí",
        "indio reproduce",
        "indio tirate",
        "indio dale",
        "indio por",
        "indio tira",
    ],
)
def test_preset2_command_verbs_still_fire(text):
    """Preset 2: all command-verb patterns remain active."""
    set_sensitivity(2)
    assert matches(text) is True


def test_preset2_bare_indio_does_not_fire():
    """Preset 2: bare 'indio' alone must not trigger."""
    set_sensitivity(2)
    assert matches("indio") is False


# ---------------------------------------------------------------------------
# Default preset is 2 (only "che indio" invokes out of the box)
# ---------------------------------------------------------------------------


def test_default_preset_is_2():
    """Out of the box the userbot runs preset 2: 'che indio' invokes but the
    'que indio'/'eh indio' false-positive variants do not."""
    assert _NS["_SENSITIVITY_PRESET"] == 2


# ---------------------------------------------------------------------------
# Switching presets restores behavior
# ---------------------------------------------------------------------------


def test_switching_back_to_preset1_restores_que_indio():
    """After switching 2 → 1, 'que indio' must match again."""
    set_sensitivity(2)
    assert matches("que indio") is False
    set_sensitivity(1)
    assert matches("que indio") is True


# ---------------------------------------------------------------------------
# Preset 3 — re-enables que/eh indio (same patterns as preset 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "che indio",
        "que indio",  # re-enabled in preset 3
        "eh indio",   # re-enabled in preset 3
        "indio ponete",
        "indio poneme",
        "indio reproduci",
        "indio reproducí",
        "indio reproduce",
        "indio tirate",
        "indio dale",
        "indio por",
        "indio tira",
    ],
)
def test_preset3_invocation_and_commands_fire(text):
    """Preset 3: 'che/que/eh indio' and all command-verb patterns fire."""
    set_sensitivity(3)
    assert matches(text) is True


def test_preset3_bare_indio_does_not_fire():
    """Preset 3: bare 'indio' alone must not trigger."""
    set_sensitivity(3)
    assert matches("indio") is False


# ---------------------------------------------------------------------------
# Grammar content for preset 2 does not include "que indio"/"eh indio"
# ---------------------------------------------------------------------------


def test_preset2_grammar_excludes_que_indio_and_eh_indio():
    """Preset 2 grammar string should not contain 'que indio' or 'eh indio'
    so VOSK is less likely to collapse noise into those phrases."""
    import json as _json

    set_sensitivity(2)
    grammar_phrases = _json.loads(_NS["_build_vosk_grammar"]())
    assert "que indio" not in grammar_phrases
    assert "eh indio" not in grammar_phrases


def test_preset1_grammar_includes_que_indio_and_eh_indio():
    """Preset 1 grammar should include 'que indio' and 'eh indio'."""
    import json as _json

    set_sensitivity(1)
    grammar_phrases = _json.loads(_NS["_build_vosk_grammar"]())
    assert "que indio" in grammar_phrases
    assert "eh indio" in grammar_phrases


# ---------------------------------------------------------------------------
# Teardown: restore preset 1 so test isolation is maintained
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_preset():
    """Reset sensitivity to the default preset (2) after each test."""
    yield
    set_sensitivity(2)


# ---------------------------------------------------------------------------
# Preset 4 — same VOSK gating as preset 2 + Whisper confirmation layer
# ---------------------------------------------------------------------------


def test_preset4_che_indio_fires():
    """Preset 4 VOSK layer: 'che indio' still triggers (same as preset 2)."""
    set_sensitivity(4)
    assert matches("che indio") is True


@pytest.mark.parametrize("text", ["que indio", "eh indio"])
def test_preset4_que_eh_indio_do_not_fire(text):
    """Preset 4 VOSK layer: 'que indio' and 'eh indio' are not active patterns."""
    set_sensitivity(4)
    assert matches(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "indio ponete",
        "indio poneme",
        "indio reproduci",
        "indio reproducí",
        "indio reproduce",
        "indio tirate",
        "indio dale",
        "indio por",
        "indio tira",
    ],
)
def test_preset4_command_verbs_still_fire(text):
    """Preset 4 VOSK layer: all command-verb patterns remain active."""
    set_sensitivity(4)
    assert matches(text) is True


def test_preset4_bare_indio_does_not_fire():
    """Preset 4: bare 'indio' alone must not trigger."""
    set_sensitivity(4)
    assert matches("indio") is False


# ---------------------------------------------------------------------------
# _whisper_confirms_indio — pure function tests
# ---------------------------------------------------------------------------

confirms = _NS["_whisper_confirms_indio"]


@pytest.mark.parametrize(
    "text",
    [
        "che indio ponete un tema",
        "ponete algo indio",
        "INDIO",
        "indio,",   # punctuation attached — "indio," contains "indio"
        "indios",   # plural also contains "indio" as substring
    ],
)
def test_whisper_confirms_indio_true(text):
    """_whisper_confirms_indio returns True when the word 'indio' is present."""
    assert confirms(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "ponete un tema de los redondos",
        "",
        "el indo",  # typo — no "indio"
        None,
    ],
)
def test_whisper_confirms_indio_false(text):
    """_whisper_confirms_indio returns False when 'indio' is absent."""
    assert confirms(text) is False
