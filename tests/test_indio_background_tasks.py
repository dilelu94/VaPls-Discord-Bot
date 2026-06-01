"""Behavior: fire-and-forget background work the indio dispatches (relay
calls, history compression, autoplay) must reach completion even when the
caller's frame returns and Python is free to garbage-collect the task object.

Without a strong reference held somewhere, CPython can GC the Task before
the event loop schedules its first step. The symptom we hit in the wild:
indio decides to play music, the dispatch task vanishes, and nothing happens
— no error, no log, just silence. The fix keeps every spawned coroutine
alive in a module-level set until it finishes.

Boundary mocked: none. We exercise the real ``_spawn`` helper end-to-end.
"""
from __future__ import annotations

import asyncio
import gc


async def test_spawned_coroutine_runs_to_completion_when_caller_drops_ref():
    """The observable promise: a coroutine handed to ``_spawn`` runs, even
    if nobody holds a reference to the returned task. This is what makes
    the fire-and-forget call sites (PLAY_MUSIC dispatch, history compress)
    safe — the indio doesn't silently lose work."""
    import geminiCommand

    done = asyncio.Event()

    async def _work():
        await asyncio.sleep(0)
        done.set()

    # Call site mirroring real usage: spawn without keeping the task.
    geminiCommand._spawn(_work())
    # Aggressively drop frame-local refs and force collection — if _spawn
    # weren't holding a strong ref internally, the task could be collected
    # here before the loop runs it.
    gc.collect()

    # Yield to the loop so the task gets to run. Bounded wait so a regression
    # to bare create_task surfaces as a timeout, not a hang.
    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert done.is_set()


async def test_spawned_task_is_tracked_until_finished_then_released():
    """The tracking set must shrink back to empty so it doesn't leak Task
    objects across the lifetime of the process. Done callbacks discard the
    finished tasks."""
    import geminiCommand

    started = asyncio.Event()
    proceed = asyncio.Event()

    async def _work():
        started.set()
        await proceed.wait()

    # Snapshot current tracked tasks (other tests may have spawned some).
    baseline = set(geminiCommand._background_tasks)
    geminiCommand._spawn(_work())

    await asyncio.wait_for(started.wait(), timeout=1.0)

    # While the task is still in flight, it must be retained.
    new_tasks = set(geminiCommand._background_tasks) - baseline
    assert len(new_tasks) == 1, "in-flight task must be held by _background_tasks"

    proceed.set()
    # Let the loop process the done callback.
    for _ in range(5):
        await asyncio.sleep(0)

    # Once finished, the task is dropped from the tracking set.
    leftover = set(geminiCommand._background_tasks) - baseline
    assert not leftover, "finished task must be released after completion"


async def test_dispatch_indio_actions_spawn_completes_without_caller_ref(
        monkeypatch):
    """End-to-end: when ``indioLogic`` spawns ``_dispatch_indio_actions``,
    the dispatched action reaches the relay call site even after the caller
    drops the task. This mirrors the real production failure mode."""
    import geminiCommand
    import playCommand
    import types
    from unittest.mock import MagicMock

    # No player so RESUME_MUSIC fails cleanly and records a status — the
    # easiest end-to-end action that doesn't need network/voice.
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    edited: list[str] = []

    async def _channel_send(content=None, **kwargs):
        pass

    class _FakeMsg:
        id = 1234
        channel = types.SimpleNamespace(id=42, send=_channel_send)

        async def edit(self, *, content=None, **kwargs):
            if content is not None:
                edited.append(content)

    handle = types.SimpleNamespace(
        via_relay=False,
        channel_id=42,
        message_id=None,
        message=_FakeMsg(),
        single=True,
    )

    # Spawn via the real helper, same call site as indioLogic.
    geminiCommand._spawn(geminiCommand._dispatch_indio_actions(
        MagicMock(), 100, [("RESUME_MUSIC", None)],
        reply_handle=handle,
        reply_text="dale, va",
    ))
    gc.collect()

    # Allow the spawned task to complete. Bounded wait — regression to bare
    # create_task can manifest as a missed edit.
    for _ in range(50):
        if edited:
            break
        await asyncio.sleep(0.01)

    assert edited, "expected the dispatched action to edit the reply message"
