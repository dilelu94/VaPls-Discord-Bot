"""Behavior: when the indio decides NOT to call a tool (because the message
is a question, charla, or doesn't meet the tool's hard requirements), it
must NOT emit the confirmation phrases it would normally use before calling
a tool ("tomá X", "dale, va Y", "salteo", "retomo"). Otherwise the indio
ends up saying "Tomá" to a question and the user sees a phantom action
that never happened.

Anchor case (2026-05-31): Miles asked "indio que onda con el pez que pescó
chalo?" — a chat question. Gemini wanted to fire play_sound but the hard
verb requirement bounced it. The model had ALREADY emitted "Tomá" as a
pre-confirmation though, so the user saw just "Tomá" with no tool call.

These tests pin the wording in INDIO_SYSTEM so accidental rewrites that
drop the "only confirm if you're actually going to call the tool" rule get
caught. They don't assert exact phrases — only the key signals.
"""
from __future__ import annotations


def _indio_system():
    from geminiCommand import INDIO_SYSTEM
    return INDIO_SYSTEM.lower()


def test_confirmation_is_gated_on_actually_calling_the_tool():
    """The rule has to explicitly say that 'tomá', 'dale va' etc. only go
    out when the tool is actually going to be invoked. Without this, the
    model emits them as decorative confirmations even when no tool will
    run."""
    system = _indio_system()
    # The new gating clause: "solo si la vas a llamar" or equivalent.
    assert (
        "solo si la vas a llamar" in system
        or "y solo si" in system
        or "solo si vas a llamar" in system
    ), "INDIO_SYSTEM no condiciona el pre-confirm a llamar la tool"


def test_no_phantom_confirmation_phrases_when_skipping_tool():
    """If Gemini decides not to call any tool, it must not say 'tomá' or
    similar. The prompt should call those phrases out as forbidden when
    no tool fires."""
    system = _indio_system()
    # The negative rule mentions the canonical phrases that must NOT leak
    # when there's no tool call.
    assert "tomá" in system  # already there as example
    # The "NO digas tomá" clause has to be present in some form.
    assert "no digas" in system
    # And it has to reference the "without tool call" scenario, framed
    # however (pregunta / charla / no cumple).
    assert "pregunta" in system or "charla" in system or "no cumple" in system
