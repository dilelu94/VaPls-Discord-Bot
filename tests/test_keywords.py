"""Behavior: `checkKeywords` decides whether a transcript should trigger a
sound. It must match any watched keyword, anywhere, case-insensitively."""
from keywords import checkKeywords


def test_matches_keyword_anywhere_in_sentence():
    assert checkKeywords("oye necesito ayuda con esto") is True


def test_match_is_case_insensitive():
    assert checkKeywords("I NEED that whistle") is True


def test_matches_each_language_family():
    assert checkKeywords("necesito") is True   # es
    assert checkKeywords("pito") is True        # es
    assert checkKeywords("i need") is True      # en
    assert checkKeywords("whistle") is True     # en


def test_no_keyword_returns_false():
    assert checkKeywords("hello world, nothing here") is False


def test_empty_string_returns_false():
    assert checkKeywords("") is False


def test_substring_match_triggers():
    # Keywords are matched as substrings (documented behavior), so a keyword
    # embedded in a longer word still triggers.
    assert checkKeywords("necesitooo") is True
