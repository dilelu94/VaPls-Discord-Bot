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


def test_action_success_suffix_has_empty_spacewar_guide():
    from geminiCommand import _ACTION_SUCCESS_SUFFIX

    assert _ACTION_SUCCESS_SUFFIX.get("SPACEWAR_GUIDE") == ""
    assert _ACTION_SUCCESS_SUFFIX.get("USE_IMAGE") == ""


async def test_dispatch_spacewar_guide_sends_without_listo_suffix():
    import types
    from unittest.mock import AsyncMock, MagicMock
    import geminiCommand

    sent_messages = []

    async def _send(content=None, **kwargs):
        sent_messages.append({"content": content, "kwargs": kwargs})

    channel = types.SimpleNamespace(id=123, send=_send)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    edited = []

    async def _edit(*, content=None, **kwargs):
        if content is not None:
            edited.append(content)

    fake_msg = types.SimpleNamespace(id=999, channel=channel, edit=_edit)

    handle = types.SimpleNamespace(
        via_relay=False,
        channel_id=123,
        message_id=None,
        message=fake_msg,
        single=True,
    )

    statuses = await geminiCommand._dispatch_indio_actions(
        bot,
        100,
        [("SPACEWAR_GUIDE", "")],
        reply_handle=handle,
        reply_text="Acá tenés la guía de Spacewar",
    )

    assert any("spacewar: ok" in s for s in statuses)
    assert len(sent_messages) == 1
    assert "Spacewar" in sent_messages[0]["content"] or "480" in sent_messages[0]["content"]
    # The initial message was NOT edited with "listo" or "✅"
    assert not edited or not any("listo" in e.lower() or "✅" in e for e in edited)


async def test_dispatch_spacewar_guide_failure_edits_reply_with_error():
    import types
    from unittest.mock import AsyncMock, MagicMock
    import geminiCommand

    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(return_value=None)

    edited = []

    async def _edit(*, content=None, **kwargs):
        if content is not None:
            edited.append(content)

    fake_msg = types.SimpleNamespace(id=999, channel=types.SimpleNamespace(id=123), edit=_edit)

    handle = types.SimpleNamespace(
        via_relay=False,
        channel_id=123,
        message_id=None,
        message=fake_msg,
        single=True,
    )

    statuses = await geminiCommand._dispatch_indio_actions(
        bot,
        100,
        [("SPACEWAR_GUIDE", "")],
        reply_handle=handle,
        reply_text="Acá tenés la guía de Spacewar",
    )

    assert any("spacewar: fail" in s for s in statuses)
    assert edited, "expected the reply message to be edited with a failure reason"
    assert "no pude mandar la guía de Spacewar" in edited[0]
    assert "listo" not in edited[0].lower()

