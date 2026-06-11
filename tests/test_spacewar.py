"""Behavioral tests for /spacewar: slash command + Indio tool integration.

Slash command covers:
- Response is ephemeral
- Response mentions Spacewar
- Response mentions steam://run/480
- Response includes Linux/Deck and Bazzite commands

Indio tool covers:
- _INDIO_TOOLS contains spacewar_guide definition
- _FUNCTION_CALL_TO_ACTION maps spacewar_guide → SPACEWAR_GUIDE
- _actions_from_function_calls translates correctly
- System prompt includes spacewar_guide triggers
"""

from conftest import sent_text, ephemeral_for


# ── Slash command tests ───────────────────────────────────────────────────


async def test_spacewar_is_ephemeral(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    assert ctx.sent_messages, "expected at least one message"
    assert ctx.sent_ephemeral[0] is True, "response should be ephemeral"


async def test_spacewar_contains_key_content(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    text = sent_text(ctx)
    assert "Spacewar" in text
    assert "steam://run/480" in text
    assert "Linux" in text or "Steam Deck" in text


async def test_spacewar_includes_linux_command(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    text = sent_text(ctx)
    assert "steam steam://run/480" in text


async def test_spacewar_includes_bazzite_command(ctx_factory):
    ctx = ctx_factory()
    from bot import spacewar

    await spacewar(ctx)

    text = sent_text(ctx)
    assert "Bazzite" in text or "flatpak" in text


# ── Indio tool tests ─────────────────────────────────────────────────────


def test_indio_tools_contains_spacewar_guide():
    from geminiCommand import _INDIO_TOOLS

    names = [t["name"] for t in _INDIO_TOOLS]
    assert "spacewar_guide" in names


def test_function_call_to_action_maps_spacewar_guide():
    from geminiCommand import _FUNCTION_CALL_TO_ACTION

    mapping = _FUNCTION_CALL_TO_ACTION.get("spacewar_guide")
    assert mapping is not None
    action, arg_key = mapping
    assert action == "SPACEWAR_GUIDE"
    assert arg_key is None


def test_actions_from_function_calls_translates_spacewar_guide():
    from geminiCommand import _actions_from_function_calls

    calls = [{"name": "spacewar_guide", "args": {}}]
    actions = _actions_from_function_calls(calls)
    assert ("SPACEWAR_GUIDE", "") in actions


def test_actions_from_function_calls_ignores_unknown_tool():
    from geminiCommand import _actions_from_function_calls

    calls = [{"name": "nonexistent_tool", "args": {}}]
    actions = _actions_from_function_calls(calls)
    assert actions == []


def test_indio_system_prompt_includes_spacewar_guide():
    from geminiCommand import INDIO_SYSTEM

    assert "spacewar_guide" in INDIO_SYSTEM
    assert (
        "steam://run/480" in INDIO_SYSTEM
        or "Spacewar" in INDIO_SYSTEM
        or "spacewar" in INDIO_SYSTEM
    )


def test_action_fallback_text_has_spacewar():
    from geminiCommand import _ACTION_FALLBACK_TEXT

    assert "SPACEWAR_GUIDE" in _ACTION_FALLBACK_TEXT
    assert "Spacewar" in _ACTION_FALLBACK_TEXT["SPACEWAR_GUIDE"]
