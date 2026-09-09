"""Behavior: the persisted indio history is scrubbed of Discord emoji codes
and stray bracketed speaker prefixes, so it stops feeding noise back to the
model on the next turn.

The visible reply to Discord still keeps the emojis (renders fine in chat),
and the model still receives the ``_format_guild_emojis`` block in the system
prompt so it knows how to emit them — what we strip is only the copy that
goes into ``_indio_history``.
"""
from __future__ import annotations

import pytest


async def test_emoji_markup_stripped_from_model_turn(
    indio, ctx_factory, patch_generate, reply_factory,
):
    """Custom emoji ``<:ahegao:765>`` in the model reply must render in
    Discord but must NOT pollute the persisted memory."""
    patch_generate(reply=reply_factory(text="jaja <:ahegao:765> posta"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indio.indioLogic(ctx, "che cómo va", nuevo=False)

    # Visible: the user sees the emoji intact so Discord renders it.
    visible = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "<:ahegao:765>" in visible

    # Stored: history is clean.
    stored = [p["text"] for t in indio._indio_history["guild-100"] for p in t["parts"]]
    assert all("<:ahegao:765>" not in s for s in stored)
    assert all("<a:" not in s and "<:" not in s for s in stored)


async def test_bare_shortcode_stripped_from_model_turn(
    indio, ctx_factory, patch_generate, reply_factory,
):
    """The model sometimes drops bare ``:ahegao:`` (which Discord won't
    render). Either way, that shortcode is noise in memory."""
    patch_generate(reply=reply_factory(text="jaja :ahegao: posta"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indio.indioLogic(ctx, "che", nuevo=False)

    stored = [p["text"] for t in indio._indio_history["guild-100"] for p in t["parts"]]
    assert all(":ahegao:" not in s for s in stored)


async def test_emoji_in_user_question_stripped_from_history(
    indio, ctx_factory, patch_generate, reply_factory,
):
    """The user can paste ``<:ahegao:765>`` in their question. That's fine
    for the current turn (Gemini gets it raw), but it doesn't get persisted."""
    patch_generate(reply=reply_factory(text="ok"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indio.indioLogic(ctx, "hola <:ahegao:765> indio", nuevo=False)

    user_turn = indio._indio_history["guild-100"][0]
    user_text = user_turn["parts"][0]["text"]
    assert "<:ahegao:765>" not in user_text
    # But the speaker identity + the wording the user typed survive.
    assert "Mati" in user_text
    assert "hola" in user_text
    assert "indio" in user_text


async def test_model_speaker_prefix_stripped_from_visible_and_stored(
    indio, ctx_factory, patch_generate, reply_factory,
):
    """If Gemini mirrors the input format and starts its reply with
    ``[Miles]: ...``, the user shouldn't see that prefix and the persisted
    memory shouldn't carry it either."""
    patch_generate(reply=reply_factory(text="[Miles]: jaja boludo"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indio.indioLogic(ctx, "che", nuevo=False)

    visible = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "jaja boludo" in visible
    assert "[Miles]:" not in visible

    model_turn = indio._indio_history["guild-100"][1]
    model_text = model_turn["parts"][0]["text"]
    assert "[Miles]:" not in model_text
    assert "jaja boludo" in model_text


async def test_discord_mentions_stripped_from_history(
    indio, ctx_factory, patch_generate, reply_factory,
):
    """User/channel/role mention markup (``<@123>``, ``<#456>``, ``<@&789>``)
    is opaque to the model. Strip it from persisted memory so it doesn't
    accumulate as noise."""
    patch_generate(reply=reply_factory(text="ok"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indio.indioLogic(
        ctx, "che <@123> mirá <#456> de <@&789>", nuevo=False,
    )

    user_text = indio._indio_history["guild-100"][0]["parts"][0]["text"]
    assert "<@123>" not in user_text
    assert "<#456>" not in user_text
    assert "<@&789>" not in user_text
    # The natural-language words around the mentions survive.
    assert "che" in user_text
    assert "mirá" in user_text


async def test_indio_self_prefix_stripped(
    indio, ctx_factory, patch_generate, reply_factory,
):
    """Existing behavior — the indio sometimes prefixes its own reply with
    ``Indio:`` or ``[indio]:``. That's still stripped."""
    patch_generate(reply=reply_factory(text="Indio: todo bien che"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indio.indioLogic(ctx, "che", nuevo=False)

    visible = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "Indio:" not in visible.split("\n")[-1]  # at start of reply at least
    model_text = indio._indio_history["guild-100"][1]["parts"][0]["text"]
    assert not model_text.lower().startswith("indio:")
    assert "todo bien che" in model_text
