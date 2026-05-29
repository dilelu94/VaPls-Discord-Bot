"""Behavior: each Gemini reply is prefixed with a header attributing the
question to the asker and quoting it, so group chat stays readable."""
import types

from geminiCommand import _format_user_header


def _ctx(author):
    return types.SimpleNamespace(author=author)


def test_uses_display_name():
    header = _format_user_header(_ctx(types.SimpleNamespace(display_name="Mati", name="mati")), "hola")
    assert "Mati" in header
    assert "> hola" in header


def test_falls_back_to_name_when_no_display_name():
    author = types.SimpleNamespace(name="mati")  # no display_name attribute
    header = _format_user_header(_ctx(author), "hola")
    assert "mati" in header


def test_falls_back_to_alguien_when_nameless():
    author = types.SimpleNamespace()  # neither display_name nor name
    header = _format_user_header(_ctx(author), "hola")
    assert "alguien" in header


def test_multiline_question_quoted_per_line():
    header = _format_user_header(
        _ctx(types.SimpleNamespace(display_name="Mati")), "primera\nsegunda")
    assert "> primera" in header
    assert "> segunda" in header


def test_empty_question_does_not_error():
    header = _format_user_header(_ctx(types.SimpleNamespace(display_name="Mati")), "")
    assert "Mati" in header
