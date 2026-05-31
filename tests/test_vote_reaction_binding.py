"""Behavior: number reactions (1️⃣…N) get added ONLY to the message that lists
the music options — never to an unrelated chat reply.

Anchor case from the 2026-05-31 logs: a vote was open from an earlier turn
("che indio, ponete un tema del Indio Solari" had listed 5 options). Then
Enrique said something dramatic, the indio replied "¡pará, Enrique!…" with
a normal chat message, and the bot added 1-5 reactions to that chat reply
because there was still a vote alive in the guild. Reactions on a
non-options message confuse the picker AND embarrass the indio.

The fix: ``_attach_vote_reactions`` refuses to bind to a second message once
the vote already has a reaction_message_id. The call sites also gate on
``reaction_message_id is None`` so they don't try to bind again.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


async def test_attach_vote_reactions_refuses_to_rebind():
    """A vote that's already pinned to its options message can't be repointed
    to a later message by a second call. Pure helper test — no Discord plumbing."""
    import playCommand
    import geminiCommand

    vote = playCommand.MusicVote(
        bot=MagicMock(), guild_id=1,
        candidates=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
        on_resolve=AsyncMock(),
    )
    # First attach: binds normally and seeds the reactions on the real message.
    msg1 = MagicMock()
    msg1.add_reaction = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=msg1)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await geminiCommand._attach_vote_reactions(bot, vote, 555, 111, 3)
    assert vote.reaction_message_id == 111
    assert msg1.add_reaction.await_count == 3   # 3 candidates seeded

    # Second attach with a DIFFERENT message id (the bug scenario): must NOT
    # repoint, and must NOT add any reactions on the unrelated message.
    msg2 = MagicMock()
    msg2.add_reaction = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=msg2)

    await geminiCommand._attach_vote_reactions(bot, vote, 555, 222, 3)
    assert vote.reaction_message_id == 111         # still pointed at msg1
    msg2.add_reaction.assert_not_awaited()        # no reactions on the chat reply
