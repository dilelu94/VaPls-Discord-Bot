"""Behavior: two indio dispatches firing concurrently for the *same*
guild must serialize their play-relay invocations so the userbot doesn't
race into firing two /play slash interactions on top of each other.
Different guilds are independent and must NOT block each other.

Boundary mocked: ``_invoke_slash_via_userbot`` (the network call). We
gate it through an ``asyncio.Event`` so the test controls exact timing.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import MagicMock


def _member_in_voice(user_id=42, channel_id=99):
    """A requester that passes the music gate in ``_dispatch_indio_actions``:
    a Discord member with an ``id`` and a ``voice.channel``. Without this the
    PLAY_MUSIC actions are gated as 'no requester' before reaching the relay."""
    return types.SimpleNamespace(
        id=user_id,
        voice=types.SimpleNamespace(channel=types.SimpleNamespace(id=channel_id)),
    )


async def test_concurrent_dispatches_same_guild_are_serialized(monkeypatch):
    """Observable promise: when dispatch_A is in the middle of a PLAY_MUSIC
    relay call and dispatch_B fires for the same guild, dispatch_B's relay
    call does NOT start until dispatch_A's finishes. Otherwise the userbot
    receives two slash invocations concurrently and Discord shows them in
    a non-deterministic order with possible side effects.
    """
    import geminiCommand

    # Track when each relay invocation enters and exits, plus the order.
    enter_events: list[str] = []
    exit_events: list[str] = []
    release_first = asyncio.Event()

    call_count = {"n": 0}

    async def _gated_relay(endpoint, channel_id, query):
        idx = call_count["n"]
        call_count["n"] += 1
        enter_events.append(f"{idx}:{query}")
        if idx == 0:
            # First caller waits for the test to release it.
            await release_first.wait()
        exit_events.append(f"{idx}:{query}")
        return True, query

    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot",
                        _gated_relay)
    monkeypatch.setattr(geminiCommand.config, "INDIO_PLAY_CHANNEL_ID", 42,
                        raising=False)

    bot = MagicMock()

    async def _channel_send(content=None, **kwargs):
        pass

    def _make_handle():
        class _FakeMsg:
            id = 1234
            channel = types.SimpleNamespace(id=42, send=_channel_send)

            async def edit(self, *, content=None, **kwargs):
                pass

        return types.SimpleNamespace(
            via_relay=False, channel_id=42, message_id=None,
            message=_FakeMsg(), single=True,
        )

    # Fire both dispatches for the same guild back-to-back.
    task_a = asyncio.create_task(geminiCommand._dispatch_indio_actions(
        bot, 100, [("PLAY_MUSIC", "song-A")],
        reply_handle=_make_handle(), reply_text="dale",
        requester_member=_member_in_voice(),
    ))
    task_b = asyncio.create_task(geminiCommand._dispatch_indio_actions(
        bot, 100, [("PLAY_MUSIC", "song-B")],
        reply_handle=_make_handle(), reply_text="dale",
        requester_member=_member_in_voice(),
    ))

    # Give both tasks a chance to start; only the first should reach the
    # relay call because the second is blocked on the per-guild lock.
    for _ in range(20):
        if enter_events:
            break
        await asyncio.sleep(0.01)

    assert enter_events == ["0:song-A"], (
        f"second dispatch leaked through the lock: {enter_events}"
    )

    # Release the first dispatch; the second should now proceed.
    release_first.set()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)

    # Both eventually ran, in the order the dispatches were queued.
    assert enter_events == ["0:song-A", "1:song-B"], enter_events
    assert exit_events == ["0:song-A", "1:song-B"], exit_events


async def test_concurrent_dispatches_different_guilds_run_in_parallel(
        monkeypatch):
    """Different guilds must remain independent: locking one guild must not
    block another. Otherwise a stuck dispatch in guild_A would freeze
    every other guild's indio."""
    import geminiCommand

    release_first = asyncio.Event()
    enter_count = {"n": 0}

    async def _gated_relay(endpoint, channel_id, query):
        my_idx = enter_count["n"]
        enter_count["n"] += 1
        if my_idx == 0:
            # First call waits — but the second is in a different guild
            # and must not be blocked.
            await release_first.wait()
        return True, query

    monkeypatch.setattr(geminiCommand, "_invoke_slash_via_userbot",
                        _gated_relay)
    monkeypatch.setattr(geminiCommand.config, "INDIO_PLAY_CHANNEL_ID", 42,
                        raising=False)

    bot = MagicMock()

    task_a = asyncio.create_task(geminiCommand._dispatch_indio_actions(
        bot, 100, [("PLAY_MUSIC", "song-A")],
        reply_handle=None, reply_text="",
        requester_member=_member_in_voice(),
    ))
    task_b = asyncio.create_task(geminiCommand._dispatch_indio_actions(
        bot, 200, [("PLAY_MUSIC", "song-B")],  # different guild
        reply_handle=None, reply_text="",
        requester_member=_member_in_voice(),
    ))

    # Yield the loop. Both should have entered the relay path (different
    # guild locks). With a regression to a single global lock, only the
    # first would enter.
    for _ in range(20):
        if enter_count["n"] >= 2:
            break
        await asyncio.sleep(0.01)

    assert enter_count["n"] == 2, (
        "second guild's dispatch was blocked by the first — locking is "
        "not per-guild as required"
    )

    release_first.set()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)
