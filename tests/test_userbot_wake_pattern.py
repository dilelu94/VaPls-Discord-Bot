"""Behavior: the VOSK wake-word matcher fires ONLY on the explicit phrases
"che indio", "indio ponete", "indio poneme", "indio reproducí" /
"indio reproduce". Bare "indio", "indio" with unrelated context, and short
utterances must NOT trigger — those were the historical sources of false
positives that saturated Whisper.

Tests the pure pattern function (``_text_matches_wake_pattern``) extracted
from ``userbot/bot.py`` so we don't need a live VOSK model.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_USERBOT_BOT = Path(__file__).resolve().parent.parent / "userbot" / "bot.py"


def _extract_pattern_matcher():
    """Pull `_normalize`, `_WAKE_PATTERNS`, and `_text_matches_wake_pattern`
    out of userbot/bot.py without executing the module's discord setup.

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
    # `_WAKE_PATTERNS` constant (a tuple literal that may span lines)
    m = re.search(
        r"^_WAKE_PATTERNS:.*?^\)\n", src, re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _WAKE_PATTERNS"
    blocks.append(m.group(0))
    # `_text_matches_wake_pattern`
    m = re.search(
        r"^def _text_matches_wake_pattern\(.*?(?=^def |\Z)", src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "could not locate _text_matches_wake_pattern"
    blocks.append(m.group(0))

    import unicodedata as _unicodedata
    ns = {"unicodedata": _unicodedata}
    exec("\n".join(blocks), ns)
    return ns["_text_matches_wake_pattern"]


matches = _extract_pattern_matcher()


# ---- positive cases -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "che indio",
    "Che indio",
    "che indio venir",  # extra words around are fine
    "bueno che indio escuchá esto",
    "indio ponete a reproducir algo",
    "indio reproduci una de los redondos",
    "indio reproducí algo",  # with accent
    "indio reproduce algo",
    "indio poneme musica",
])
def test_pattern_fires(text):
    assert matches(text) is True


# ---- negative cases (bare or unrelated context) ---------------------------

@pytest.mark.parametrize("text", [
    "",
    "indio",
    "el indio",
    "indio loco",            # historical false-positive: "indio + any word"
    "indio dale",
    "como andas",
    "che boludo",            # "che" alone, no "indio"
    "che indio ",             # trailing whitespace — still fires? actually yes
    "reproducí algo",        # missing "indio"
    "ponete a reproducir",    # missing "indio"
])
def test_pattern_does_not_fire_on_bare_or_unrelated(text):
    # The trailing-whitespace case is actually a valid hit; filter it out
    # of the negative assertion. Keep the entry to document the edge case.
    if text.strip() == "che indio":
        return
    assert matches(text) is False
