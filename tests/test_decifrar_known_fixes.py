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


def test_decifrar_prompt_pins_number_and_imperative_preservation():
    """The system prompt explicitly instructs the model to keep literal
    digits and imperative verbs. Without this, transcripts like "Indio,
    tirala 4" got rewritten to "tirala" (digit lost) — which made vote
    bridging impossible because there was no number left to parse."""
    from geminiCommand import DECIFRAR_SYSTEM
    low = DECIFRAR_SYSTEM.lower()
    # Numbers
    assert ("número" in low or "numero" in low or "dígito" in low
            or "digito" in low)
    assert "tirala" in low or "ponela" in low      # examples in the prompt
    # Imperative mood
    assert "imperativ" in low
    assert ("tirate" in low or "ponete" in low)
    # Examples calling out the inverted-tense failure mode.
    assert "tiraste" in low or "puse" in low


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


def test_prompt_teaches_command_verb_few_shot():
    """Whisper rompe los verbos imperativos del indio cuando se pegan al
    wake-word ("Indio de tener a música" en vez de "che indio, detené la
    música"). Sin ejemplos few-shot, Gemini pasaba el verbo roto a las tools
    y elegía la equivocada. El system prompt ahora le enseña los patrones
    canónicos de cada comando."""
    from geminiCommand import DECIFRAR_SYSTEM
    low = DECIFRAR_SYSTEM.lower()
    # Caso testigo del bug original (stop_music con verbo partido por Whisper).
    assert "de tener" in low
    assert "detené" in low


async def test_de_tener_witness_case_reaches_gemini_unmangled(monkeypatch):
    """El caso testigo concreto: 'Indio de tener a música' (Whisper) tiene que
    llegar a Gemini sin mutilar por _apply_known_fixes — la decisión es
    enseñarle a Gemini con few-shot, no agregar substring fixes que romperían
    'dejar de tener' en castellano natural. Si alguien agrega 'de tener' a
    la tabla en el futuro, este test se pone rojo."""
    import geminiCommand
    import geminiClient

    seen_input = {}

    async def fake_generate(*, user_message, system_instruction, **kwargs):
        seen_input["text"] = user_message
        seen_input["system"] = system_instruction
        reply = MagicMock()
        reply.text = "che indio, detené la música"
        return reply

    monkeypatch.setattr(geminiClient, "generate", AsyncMock(side_effect=fake_generate))

    out = await geminiCommand.decifrarTranscripcion("Indio de tener a música")

    # El input llega a Gemini con "de tener" intacto (no fue reescrito por la tabla).
    assert "de tener" in seen_input["text"].lower()
    # El system prompt incluye la lección que le permite a Gemini resolverlo.
    assert "de tener" in seen_input["system"].lower()
    assert "detené" in seen_input["system"].lower()
    # Y la respuesta canónica del modelo pasa intacta al caller.
    assert "detené" in out.lower()


async def test_legitimate_de_tener_phrase_is_not_mutilated(monkeypatch):
    """'de tener' es una construcción legítima del castellano ("dejar de
    tener razón"). decifrar corre sobre TODA transcripción del userbot, no
    solo comandos — un substring fix la rompería en charla libre. Por eso la
    corrección vive en el prompt (few-shot, decide caso a caso), no en la
    tabla. Este test blinda esa decisión."""
    import geminiCommand
    import geminiClient

    seen_input = {}

    async def fake_generate(*, user_message, **kwargs):
        seen_input["text"] = user_message
        reply = MagicMock()
        reply.text = user_message  # echo
        return reply

    monkeypatch.setattr(geminiClient, "generate", AsyncMock(side_effect=fake_generate))

    raw = "el indio nunca va a dejar de tener razón"
    await geminiCommand.decifrarTranscripcion(raw)

    # Lo que llega a Gemini es exactamente lo que vino del Whisper.
    assert seen_input["text"] == raw


@pytest.mark.parametrize("canonical_verb", [
    "pará",
    "poné",
    "pausá",
    "seguí",
    "pasá",
    "saltá",
    "tirate",
])
def test_prompt_teaches_each_command_verb(canonical_verb):
    """Cobertura por verbo de tool del indio. Si alguien borra un ejemplo del
    few-shot, este test indica exactamente cuál falta. No verificamos conteo
    de ejemplos ni estructura del prompt — solo que cada verbo imperativo
    canónico está enseñado en algún lugar del system prompt."""
    from geminiCommand import DECIFRAR_SYSTEM
    assert canonical_verb in DECIFRAR_SYSTEM.lower(), (
        f"DECIFRAR_SYSTEM no enseña la forma imperativa {canonical_verb!r}; "
        f"sin ese ejemplo, Gemini no sabe reconstruir comandos rotos por Whisper."
    )
