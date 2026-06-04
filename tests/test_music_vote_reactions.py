"""Behavior: the /play picker is a reaction vote — the bot posts the options
and seeds 1️⃣/2️⃣/3️⃣ reactions, and the result is decided by what people react
with. Two anchors define the timing the user agreed on:

- if nobody reacts within the 1-minute hard cap, the top fuzzy-match candidate
  (candidates[0]) wins by default;
- the picker stays open for the full duration of the hard cap regardless of when
  votes are cast (the sliding window behavior was removed).

Tests run with tiny timing knobs (well under a real second) so the suite stays
fast. They assert on the outcome (which candidate ended up in ``on_resolve``,
how many vote counts were tallied) rather than on the internal close-task
plumbing, so the timer implementation can be swapped without breaking them.
"""
from __future__ import annotations

import asyncio

import pytest


def _cands(n: int = 3) -> list[dict]:
    return [{"id": f"id{i}", "title": f"Tema {chr(65 + i)}",
             "duration_string": "3:00"} for i in range(n)]


@pytest.fixture
def clear_active_votes():
    """Active votes are a module-level registry — wipe it so tests don't bleed
    state into each other (a leftover from a previous test would block
    ``open_music_vote`` or pollute the reaction dispatcher's search)."""
    import playCommand
    playCommand.active_votes.clear()
    yield
    # Cancel any timers still in flight so the asyncio loop closes cleanly.
    for v in list(playCommand.active_votes.values()):
        task = v._close_task
        if task is not None and not task.done():
            task.cancel()
    playCommand.active_votes.clear()


async def _wait_for_close(vote, timeout: float = 1.0) -> None:
    """Wait until ``vote`` reports closed (or fail the test on timeout)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not vote.closed:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("vote did not close in time")
        await asyncio.sleep(0.005)


async def test_no_votes_first_option_wins_by_default(clear_active_votes):
    """Nobody reacts within the hard cap → candidates[0] is resolved. This is
    the "indio asked for music, room is silent, just play the best match"
    fallback that the picker promises."""
    import playCommand
    resolved: list[dict] = []

    async def _on_resolve(vote, winner):
        resolved.append(winner)

    candidates = _cands(3)
    vote = playCommand.open_music_vote(
        bot=None, guild_id=42, candidates=candidates,
        on_resolve=_on_resolve, vote_max_sec=0.05, vote_window_sec=10.0,
    )
    vote.start_timeout()
    await _wait_for_close(vote)

    assert resolved == [candidates[0]]


async def test_first_vote_arms_settle_window_and_wins(clear_active_votes):
    """A single reaction lands, then silence. The vote stays open until the
    hard cap elapses (sliding window removed)."""
    import playCommand
    resolved: list[dict] = []

    async def _on_resolve(vote, winner):
        resolved.append(winner)

    candidates = _cands(3)
    # Use a small max_sec so the test stays fast
    vote = playCommand.open_music_vote(
        bot=None, guild_id=42, candidates=candidates,
        on_resolve=_on_resolve, vote_max_sec=0.1,
    )
    vote.start_timeout()
    assert vote.register_vote(user_id=7, idx=2)
    # Should NOT be closed immediately
    await asyncio.sleep(0.02)
    assert not vote.closed
    
    await _wait_for_close(vote, timeout=0.2)
    assert resolved == [candidates[2]]


async def test_majority_wins_when_multiple_users_vote(clear_active_votes):
    """Two people pick option B, one picks option C → B wins. The picker has
    to actually tally votes, not just "first vote wins"."""
    import playCommand
    resolved: list[dict] = []

    async def _on_resolve(vote, winner):
        resolved.append(winner)

    candidates = _cands(3)
    vote = playCommand.open_music_vote(
        bot=None, guild_id=42, candidates=candidates,
        on_resolve=_on_resolve, vote_max_sec=0.1,
    )
    vote.start_timeout()
    vote.register_vote(user_id=1, idx=1)
    vote.register_vote(user_id=2, idx=2)
    vote.register_vote(user_id=3, idx=1)
    await _wait_for_close(vote, timeout=0.2)

    assert resolved == [candidates[1]]





async def test_reaction_dispatcher_lands_vote_on_picker_message(clear_active_votes):
    """The bot-level reaction handler routes a 1️⃣/2️⃣ tap on the picker message
    into the right MusicVote, using the channel+message id binding the picker
    set when it posted the prompt."""
    import playCommand
    import geminiCommand

    candidates = _cands(3)
    vote = playCommand.open_music_vote(
        bot=None, guild_id=42, candidates=candidates,
        on_resolve=lambda v, w: None, vote_max_sec=10.0, vote_window_sec=10.0,
    )
    vote.reaction_message_id = 9001
    vote.reaction_channel_id = 7

    accepted = geminiCommand.register_reaction_vote(
        channel_id=7, message_id=9001, emoji="2️⃣", user_id=55,
    )
    assert accepted is True
    assert vote.votes == {55: 1}


async def test_reaction_on_unrelated_message_is_ignored(clear_active_votes):
    """A keycap reaction on some other message must not register against the
    open vote — only reactions on the picker message itself count."""
    import playCommand
    import geminiCommand

    candidates = _cands(3)
    vote = playCommand.open_music_vote(
        bot=None, guild_id=42, candidates=candidates,
        on_resolve=lambda v, w: None, vote_max_sec=10.0, vote_window_sec=10.0,
    )
    vote.reaction_message_id = 9001
    vote.reaction_channel_id = 7

    accepted = geminiCommand.register_reaction_vote(
        channel_id=7, message_id=12345, emoji="1️⃣", user_id=55,
    )
    assert accepted is False
    assert vote.votes == {}
