"""Behavior: a hand-curated table of phonetic-confusion fixes is applied
to transcripts BEFORE they reach Gemini, so recurring ASR mistakes that we
know about (e.g. "líneas horarias" for "Indio Solari") get corrected
deterministically without spending Gemini calls on them.

Tests anchor on the public entry point (``decifrarTranscripcion``) and a
fake Gemini boundary — they don't assert exactly which substitutions exist,
only that the canonical "Indio Solari" case is fixed and that unrelated
text passes through unchanged."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clear_decifrar_cache():
    import geminiCommand
    geminiCommand._decifrar_cache.clear()
    yield
    geminiCommand._decifrar_cache.clear()


async def test_lineas_horarias_is_rewritten_to_indio_solari(monkeypatch):
    """The classic phonetic confusion: Whisper hears 'Indio Solari' as
    'líneas horarias'. The fix table rewrites it before Gemini sees the text."""
    import geminiCommand
    import geminiClient

    seen_input = {}

    async def fake_generate(*, user_message, **kwargs):
        seen_input["text"] = user_message
        # Gemini just echoes what it was given — the value of this test is
        # the rewrite happening upstream, before Gemini even runs.
        reply = MagicMock()
        reply.text = user_message
        return reply

    monkeypatch.setattr(geminiClient, "generate", AsyncMock(side_effect=fake_generate))

    out = await geminiCommand.decifrarTranscripcion(
        "ponete un tema de líneas horarias"
    )

    # Gemini received the corrected text…
    assert "indio solari" in seen_input["text"].lower()
    assert "líneas horarias" not in seen_input["text"].lower()
    # …and the returned value reflects the correction too.
    assert "indio solari" in out.lower()


async def test_unrelated_text_passes_through_unchanged(monkeypatch):
    """Text that doesn't contain any known confusion is not modified by the
    fix table — Gemini sees it verbatim and the function returns whatever
    Gemini returned."""
    import geminiCommand
    import geminiClient

    seen_input = {}

    async def fake_generate(*, user_message, **kwargs):
        seen_input["text"] = user_message
        reply = MagicMock()
        reply.text = user_message
        return reply

    monkeypatch.setattr(geminiClient, "generate", AsyncMock(side_effect=fake_generate))

    raw = "ponete bizarrap session 53"
    out = await geminiCommand.decifrarTranscripcion(raw)

    assert seen_input["text"] == raw   # no preprocessing applied
    assert out == raw
