"""Unit tests for ``_strip_speaker_prefix`` — the function that scrubs any
"[Name]:" / "(Name):" / "Indio:" prefix the model sometimes mirrors at the
start of its reply, despite the system prompt telling it not to.
"""
from __future__ import annotations

import pytest

from geminiCommand import _strip_speaker_prefix


def test_strips_indio_self_prefix_with_brackets():
    assert _strip_speaker_prefix("[indio]: todo bien che") == "todo bien che"


def test_strips_indio_self_prefix_bareword():
    assert _strip_speaker_prefix("Indio: todo bien che") == "todo bien che"


def test_strips_indio_self_prefix_with_parens_and_dash():
    assert _strip_speaker_prefix("(el indio) - todo bien") == "todo bien"


def test_strips_other_speaker_bracketed_prefix():
    """The model imitates the user turn format and emits "[Miles]:" as if it
    were Miles. Strip it."""
    assert _strip_speaker_prefix("[Miles]: jaja boludo") == "jaja boludo"


def test_strips_other_speaker_parens_prefix():
    assert _strip_speaker_prefix("(Miles): jaja boludo") == "jaja boludo"


def test_leaves_brackets_mid_sentence_alone():
    """Brackets in the middle of a reply are real content, not a prefix."""
    text = "che el [audio] no anda"
    assert _strip_speaker_prefix(text) == text


def test_leaves_inline_colon_alone():
    """Colons inside a reply are fine — only a *leading* prefix gets stripped."""
    text = "te juro: posta posta"
    assert _strip_speaker_prefix(text) == text


def test_empty_and_none_safe():
    assert _strip_speaker_prefix("") == ""
    assert _strip_speaker_prefix(None) is None


def test_leading_whitespace_is_consumed():
    """A reply that starts with whitespace + prefix still gets cleaned."""
    assert _strip_speaker_prefix("   [Miles]: hola") == "hola"


def test_does_not_overrun_into_second_line():
    """The regex must stop at the first newline so a multi-line reply keeps
    its body intact."""
    out = _strip_speaker_prefix("[Miles]: primera\nsegunda")
    assert out == "primera\nsegunda"
