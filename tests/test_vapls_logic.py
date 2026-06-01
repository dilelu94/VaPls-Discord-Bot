"""Behavior: /vapls asks Gemini once (stateless) and posts the reply to the
user, prefixed with the asker's header. Failures become a friendly message and
never propagate as an unhandled exception."""
from unittest.mock import AsyncMock

import pytest

from geminiClient import GeminiError
from geminiCommand import vaplsLogic

import geminiCommand as gc

LIMIT = gc._DISCORD_CHUNK_LIMIT


def joined(ctx):
    return "\n".join(m for m in ctx.sent_messages if m is not None)


async def test_success_posts_reply_with_header(ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="Paris es la capital"))
    ctx = ctx_factory(display_name="Mati")
    await vaplsLogic(ctx, "cual es la capital de francia")
    out = joined(ctx)
    assert "Paris es la capital" in out
    assert "Mati" in out                       # header attribution
    assert "> cual es la capital" in out        # quoted question


async def test_long_reply_sent_in_multiple_messages(ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="x" * (LIMIT * 2)))
    ctx = ctx_factory()
    await vaplsLogic(ctx, "decime algo largo")
    assert len(ctx.sent_messages) > 1


async def test_gemini_error_shows_friendly_message(ctx_factory, patch_generate):
    patch_generate(error=GeminiError("timed out", kind="timeout"))
    ctx = ctx_factory()
    await vaplsLogic(ctx, "hola")            # must not raise
    out = joined(ctx)
    assert out.strip()                        # something was shown
    assert "x" * (LIMIT) not in out           # not a model reply


async def test_unexpected_error_shows_generic_message(ctx_factory, patch_generate):
    patch_generate(error=RuntimeError("kaboom"))
    ctx = ctx_factory()
    await vaplsLogic(ctx, "hola")            # must not raise
    assert "Algo se rompió" in joined(ctx)


async def test_send_failure_is_swallowed(ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="hola"))
    ctx = ctx_factory()
    ctx.followup.send = AsyncMock(side_effect=RuntimeError("discord down"))
    # Should complete without raising even though delivery fails.
    await vaplsLogic(ctx, "hola")


async def test_key_rotation_shows_transient_notice_then_clean_reply(
    ctx_factory, patch_generate, reply_factory,
):
    # Cuando geminiClient rota de key tras un 429, /vapls debe avisarle al
    # usuario que está cambiando de key y luego reemplazar ese aviso por la
    # respuesta final — sin dejar dos mensajes en cascada.
    patch_generate(reply=reply_factory(text="Paris es la capital"), retries=1)
    ctx = ctx_factory(display_name="Mati")

    await vaplsLogic(ctx, "cual es la capital de francia")

    # El aviso transitorio apareció en algún momento.
    assert any("cambiando de key" in (c or "") for c in ctx.deferred_history)
    # El estado final NO muestra el aviso — fue reemplazado por la respuesta.
    final = joined(ctx)
    assert "cambiando de key" not in final
    assert "Paris es la capital" in final
    assert "Mati" in final


async def test_no_rotation_keeps_followup_only(
    ctx_factory, patch_generate, reply_factory,
):
    # Sin rotación, el flujo normal no toca el deferred (mantiene el spinner
    # nativo de Discord hasta que llega la respuesta vía followup).
    patch_generate(reply=reply_factory(text="hola"))
    ctx = ctx_factory()

    await vaplsLogic(ctx, "hola")

    assert ctx.deferred_history == []
    assert "hola" in joined(ctx)
