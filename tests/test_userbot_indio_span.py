"""Behavior: preset 4 isolates the exact "indio" audio using VOSK word
timestamps, then slices that window out of the segment buffer for Whisper.

Tests the two pure helpers (``_extract_indio_span`` and ``_slice_wake_audio``)
extracted from ``userbot/bot.py`` so we don't import the discord-side module.
"""
from __future__ import annotations

import re
import typing
import unicodedata as _unicodedata
from pathlib import Path


_USERBOT_BOT = Path(__file__).resolve().parent.parent / "userbot" / "bot.py"


def _extract_span_helpers():
    src = _USERBOT_BOT.read_text()

    def grab(name: str) -> str:
        """Capture a top-level function by INDENTATION: the def line plus all
        following blank/indented lines, stopping at the next unindented line.
        (Both target functions use single-line signatures, so no wrapped
        ``)`` continuation line trips up the indentation boundary.)"""
        m = re.search(rf"^def {name}\(.*?\n(?:(?:[ \t].*)?\n)*", src, re.MULTILINE)
        assert m, f"could not locate {name}"
        return m.group(0)

    blocks = [grab("_normalize")]
    m = re.search(r"^_BYTES_PER_SECOND_16K = .*\n", src, re.MULTILINE)
    assert m, "could not locate _BYTES_PER_SECOND_16K"
    blocks.append(m.group(0))
    blocks.append(grab("_extract_indio_span"))
    blocks.append(grab("_slice_wake_audio"))

    ns = {"unicodedata": _unicodedata, "Optional": typing.Optional}
    exec("\n".join(blocks), ns)
    return ns


_NS = _extract_span_helpers()
extract_span = _NS["_extract_indio_span"]
slice_audio = _NS["_slice_wake_audio"]
BPS = _NS["_BYTES_PER_SECOND_16K"]


# ---- _extract_indio_span --------------------------------------------------

def test_span_found_for_indio_word():
    result = {
        "text": "che indio ponete",
        "result": [
            {"word": "che", "start": 0.30, "end": 0.55},
            {"word": "indio", "start": 0.55, "end": 0.95},
            {"word": "ponete", "start": 0.95, "end": 1.40},
        ],
    }
    assert extract_span(result) == (0.55, 0.95)


def test_span_matches_indio_substring_token():
    result = {"result": [{"word": "indios", "start": 1.0, "end": 1.5}]}
    assert extract_span(result) == (1.0, 1.5)


def test_span_none_when_no_indio_word():
    result = {"result": [{"word": "ponete", "start": 0.0, "end": 0.4}]}
    assert extract_span(result) is None


def test_span_none_without_word_timings():
    assert extract_span({"text": "che indio"}) is None
    assert extract_span({"alternatives": [{"text": "che indio"}]}) is None
    assert extract_span(None) is None
    assert extract_span({}) is None


# ---- _slice_wake_audio ----------------------------------------------------

def test_slice_extracts_padded_window():
    seg = b"\x01\x02" * 24000  # ~1.5s of dummy 16-bit samples
    out = slice_audio(seg, (0.5, 0.9))  # default pad 0.25 → [0.25s, 1.15s]
    assert out == seg[int(0.25 * BPS): int(1.15 * BPS)]
    assert len(out) % 2 == 0  # 16-bit aligned


def test_slice_clips_to_segment_bounds():
    seg = b"\x00\x00" * 16000  # 1.0s (32000 bytes)
    out = slice_audio(seg, (0.8, 5.0))  # end far beyond the clip
    assert out == seg[int(0.55 * BPS):]  # start-pad onward, clipped to len
    assert len(out) > 0


def test_slice_empty_when_window_past_segment():
    seg = b"\x00\x00" * 16000  # 1.0s
    assert slice_audio(seg, (5.0, 5.1)) == b""
