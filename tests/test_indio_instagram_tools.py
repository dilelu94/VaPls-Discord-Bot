"""Behavior: the Indio's Instagram DM/comment replies are generated WITHOUT
Discord tools. _INDIO_TOOLS (play_music, generate_image, etc.) can only be
dispatched from Discord; on Instagram they made Gemini sometimes answer with a
bare functionCall and no text, which fell through to a silent "..." reply.

This pins the request payload sent to the Gemini boundary (tools absent), which
is the behavior that keeps IG replies textual."""

from unittest.mock import MagicMock

import pytest

USERS_STUB = {111: {"name": "Loqui", "instagram": "loqui.ig"}}


class _Guild:
    id = 42
    emojis = []


def _bot():
    b = MagicMock()
    b.guilds = [_Guild()]
    return b


@pytest.fixture
def ig_users(monkeypatch):
    import users

    monkeypatch.setattr(users, "USERS", USERS_STUB)


async def test_ig_dm_reply_does_not_send_discord_tools(
    indio, ig_users, patch_generate, reply_factory
):
    from geminiCommand import indioInstagramScraperLogic

    calls = patch_generate(reply=reply_factory(text="che, dale"))
    result = await indioInstagramScraperLogic(
        sender_username="loqui.ig",
        pregunta="como andas?",
        reel_caption="",
        bot=_bot(),
    )

    assert calls[0].get("tools") is None
    assert result["reply"] == "che, dale"


async def test_ig_comment_reply_does_not_send_discord_tools(
    indio, ig_users, monkeypatch, patch_generate, reply_factory
):
    import config
    from geminiCommand import indioInstagramCommentLogic

    monkeypatch.setattr(config, "INSTAGRAM_PAGE_TOKEN", "", raising=False)
    calls = patch_generate(reply=reply_factory(text="gracias che"))
    await indioInstagramCommentLogic(
        sender_username="loqui.ig",
        pregunta="muy buen reel",
        comment_id="17800000000000000",
        bot=_bot(),
    )

    assert calls[0].get("tools") is None
