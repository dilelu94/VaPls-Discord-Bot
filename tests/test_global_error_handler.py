"""Behavior: the global slash-command error handler always tells the user
*something* useful and never lets the interaction hang. Mocks only at the
Discord boundary (ctx.respond / ctx.followup.send).
"""
import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

import analytics
import errorHandler


def make_ctx(*, response_done=False, command_name="dummy"):
    ctx = MagicMock(name="ApplicationContext")
    ctx.author = types.SimpleNamespace(id=1, display_name="t", name="t")
    ctx.guild = types.SimpleNamespace(id=100)
    ctx.command = types.SimpleNamespace(name=command_name)

    ctx.response = MagicMock()
    ctx.response.is_done = MagicMock(return_value=response_done)

    ctx.respond = AsyncMock()
    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _text_sent(ctx) -> str:
    """All the text the user would have seen, regardless of which path."""
    chunks = []
    for call in ctx.respond.call_args_list:
        chunks.append(call.args[0] if call.args else call.kwargs.get("content", ""))
    for call in ctx.followup.send.call_args_list:
        chunks.append(call.args[0] if call.args else call.kwargs.get("content", ""))
    return "\n".join(str(c) for c in chunks)


def _was_ephemeral(ctx) -> bool:
    """Both code paths should mark the message ephemeral."""
    if ctx.respond.called:
        return ctx.respond.call_args.kwargs.get("ephemeral") is True
    if ctx.followup.send.called:
        return ctx.followup.send.call_args.kwargs.get("ephemeral") is True
    return False


@pytest.mark.asyncio
async def test_network_error_shows_connection_message_and_captures():
    ctx = make_ctx(command_name="play")
    await errorHandler.handle(ctx, asyncio.TimeoutError())

    text = _text_sent(ctx).lower()
    assert "conectarme" in text or "servicio" in text
    assert _was_ephemeral(ctx)

    # Analytics is stubbed by conftest; we just check it was told about it
    # with a network-flavored kind so the dashboard can group these.
    analytics.capture_exception.assert_called_once()
    kind = analytics.capture_exception.call_args.kwargs["properties"]["error_kind"]
    assert kind == "network"


@pytest.mark.asyncio
async def test_aiohttp_client_error_also_classified_as_network():
    ctx = make_ctx()
    await errorHandler.handle(ctx, aiohttp.ClientError("boom"))
    assert analytics.capture_exception.call_args.kwargs["properties"]["error_kind"] == "network"


@pytest.mark.asyncio
async def test_forbidden_tells_user_about_permissions():
    ctx = make_ctx()
    resp = MagicMock(status=403, reason="Forbidden")
    await errorHandler.handle(ctx, discord.Forbidden(resp, "nope"))

    assert "permisos" in _text_sent(ctx).lower()
    assert analytics.capture_exception.call_args.kwargs["properties"]["error_kind"] == "forbidden"


@pytest.mark.asyncio
async def test_generic_exception_falls_through_to_unhandled_message():
    ctx = make_ctx()
    await errorHandler.handle(ctx, RuntimeError("boom"))

    text = _text_sent(ctx).lower()
    # Whatever the wording, it must not be empty and must say *something*
    # that sounds like a generic error, not leak the exception text.
    assert text.strip()
    assert "boom" not in text
    assert analytics.capture_exception.call_args.kwargs["properties"]["error_kind"] == "unhandled"


@pytest.mark.asyncio
async def test_uses_followup_when_response_already_done():
    ctx = make_ctx(response_done=True)
    await errorHandler.handle(ctx, RuntimeError("x"))

    assert ctx.followup.send.called
    assert not ctx.respond.called


@pytest.mark.asyncio
async def test_uses_respond_when_response_not_yet_done():
    ctx = make_ctx(response_done=False)
    await errorHandler.handle(ctx, RuntimeError("x"))

    assert ctx.respond.called
    assert not ctx.followup.send.called


@pytest.mark.asyncio
async def test_unwraps_application_command_invoke_error():
    """py-cord wraps callback errors in ApplicationCommandInvokeError —
    the handler must classify based on the *original* exception."""
    ctx = make_ctx()
    wrapper = types.SimpleNamespace(original=asyncio.TimeoutError())
    await errorHandler.handle(ctx, wrapper)

    assert analytics.capture_exception.call_args.kwargs["properties"]["error_kind"] == "network"


@pytest.mark.asyncio
async def test_user_still_gets_message_when_analytics_blows_up():
    """Analytics is best-effort; a failure there must not silence the user."""
    analytics.capture_exception.side_effect = RuntimeError("posthog down")
    ctx = make_ctx()

    await errorHandler.handle(ctx, RuntimeError("boom"))

    assert ctx.respond.called or ctx.followup.send.called
    assert _text_sent(ctx).strip()


@pytest.mark.asyncio
async def test_handler_swallows_discord_send_failure():
    """If the interaction expired, sending will raise — handler must not
    propagate (would crash the event loop)."""
    ctx = make_ctx()
    ctx.respond.side_effect = RuntimeError("interaction expired")

    await errorHandler.handle(ctx, RuntimeError("boom"))  # must not raise
