"""Behavior: a /vapls reply is only publicly visible in the bot's allowed
channels (INDIO_REPLY_CHANNEL_ID and INDIO_PLAY_CHANNEL_ID). Anywhere else the
reply — success OR error — must be ephemeral, so it's seen only by the invoker
and never leaks into channels that aren't the bot's."""

import pytest

import config
from conftest import ephemeral_for
from geminiClient import GeminiError
from geminiCommand import vaplsLogic

REPLY_CHANNEL = 1490008278275461280  # config.INDIO_REPLY_CHANNEL_ID
PLAY_CHANNEL = 451607097432604672  # config.INDIO_PLAY_CHANNEL_ID
STORY_CHANNEL = 451580655650996236  # config.INDIO_STORY_CHANNEL_ID
OTHER_CHANNEL = 777  # any non-allowed channel

REPLY = "Paris es la capital"


def test_allowlist_matches_the_known_channels():
    # Guards against config drift silently widening where the bot may post
    # publicly. The three channels are: reply, play, and story.
    assert config.PUBLIC_ALLOWED_CHANNEL_IDS == {
        REPLY_CHANNEL,
        PLAY_CHANNEL,
        STORY_CHANNEL,
    }


async def test_reply_public_in_reply_channel(
    ctx_factory, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text=REPLY))
    ctx = ctx_factory(channel_id=REPLY_CHANNEL)
    await vaplsLogic(ctx, "cual es la capital de francia")
    assert ephemeral_for(ctx, REPLY) is False


async def test_reply_public_in_play_channel(ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text=REPLY))
    ctx = ctx_factory(channel_id=PLAY_CHANNEL)
    await vaplsLogic(ctx, "cual es la capital de francia")
    assert ephemeral_for(ctx, REPLY) is False


async def test_reply_ephemeral_in_other_channel(
    ctx_factory, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text=REPLY))
    ctx = ctx_factory(channel_id=OTHER_CHANNEL)
    await vaplsLogic(ctx, "cual es la capital de francia")
    # The model text reached the user, but only them.
    assert ephemeral_for(ctx, REPLY) is True


async def test_long_reply_fully_ephemeral_in_other_channel(
    ctx_factory, patch_generate, reply_factory
):
    # A multi-chunk reply must not have any chunk leak publicly.
    limit = __import__("geminiCommand")._DISCORD_CHUNK_LIMIT
    patch_generate(reply=reply_factory(text="z" * (limit * 2)))
    ctx = ctx_factory(channel_id=OTHER_CHANNEL)
    await vaplsLogic(ctx, "decime algo largo")
    assert len(ctx.sent_messages) > 1
    assert all(ctx.sent_ephemeral)


async def test_unexpected_error_ephemeral_in_other_channel(ctx_factory, patch_generate):
    patch_generate(error=RuntimeError("boom"))
    ctx = ctx_factory(channel_id=OTHER_CHANNEL)
    await vaplsLogic(ctx, "hola")  # must not raise
    # Whatever error text the user saw, it was private in a non-allowed channel.
    assert ctx.sent_messages, "expected an error message to the user"
    assert all(ctx.sent_ephemeral)


async def test_gemini_error_ephemeral_in_other_channel(ctx_factory, patch_generate):
    patch_generate(error=GeminiError("timed out", kind="timeout"))
    ctx = ctx_factory(channel_id=OTHER_CHANNEL)
    await vaplsLogic(ctx, "hola")
    assert ctx.sent_messages
    assert all(ctx.sent_ephemeral)
