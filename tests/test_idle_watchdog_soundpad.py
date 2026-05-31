"""Behavior: a live /soundpad panel must keep the bot in voice.

The idle watchdog is now the single source of truth for disconnecting from
voice. Its contract widened: "active" no longer means just is_playing or
is_paused — it also means "there's a soundpad panel the user might click".

These tests pin that promise so a future refactor can't silently drop the
panel-aware check and start kicking the bot out from under an open UI.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class FakeVC:
    """Stub VoiceClient covering only the surface idleWatchdog reads."""
    def __init__(self, guild_id=100, playing=False, paused=False, connected=True):
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=999, name="general")
        self._playing = playing
        self._paused = paused
        self._connected = connected
        self.disconnect = AsyncMock(side_effect=self._on_disconnect)
        self.cleanup = MagicMock()

    def _on_disconnect(self, force=False):
        self._connected = False

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def is_connected(self):
        return self._connected


def make_bot(vc):
    bot = MagicMock()
    bot.voice_clients = [vc] if vc is not None else []
    bot.get_guild = MagicMock(return_value=SimpleNamespace(id=vc.guild.id if vc else 0))
    return bot


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test gets a clean watchdog registry, guildPlayers, and panel
    registry so nothing leaks between cases."""
    import idleWatchdog
    import soundpadCommand
    from playCommand import guildPlayers
    for task in list(idleWatchdog._watchdogs.values()):
        if not task.done():
            task.cancel()
    idleWatchdog._watchdogs.clear()
    guildPlayers.clear()
    soundpadCommand._active_panels.clear()
    yield
    for task in list(idleWatchdog._watchdogs.values()):
        if not task.done():
            task.cancel()
    idleWatchdog._watchdogs.clear()
    guildPlayers.clear()
    soundpadCommand._active_panels.clear()


async def _wait_done(task, timeout=2.0):
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        pass


async def test_active_soundpad_panel_keeps_bot_in_voice():
    """When a soundpad panel is registered as live, even an otherwise-idle
    voice client must NOT be disconnected. The watchdog defers to the panel."""
    import idleWatchdog
    import soundpadCommand

    vc = FakeVC(guild_id=100, playing=False, paused=False)
    bot = make_bot(vc)
    soundpadCommand._register_panel(100)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.05, poll_interval=0.01,
    )
    await asyncio.sleep(0.25)  # several poll cycles, well past idle_timeout
    idleWatchdog.stop_idle_watchdog(100)
    await _wait_done(task)

    assert vc.disconnect.await_count == 0, \
        "watchdog must not drop the bot while a panel is live"


async def test_panel_expiry_then_idle_disconnects():
    """When the panel goes away (e.g. View timed out), the watchdog should
    catch the now-fully-idle vc on the next poll and disconnect."""
    import idleWatchdog
    import soundpadCommand

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)
    soundpadCommand._register_panel(100)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.05, poll_interval=0.01,
    )
    # While the panel is alive, no disconnect.
    await asyncio.sleep(0.15)
    assert vc.disconnect.await_count == 0

    # Panel expires.
    soundpadCommand._unregister_panel(100)
    # Allow the watchdog to observe and tick past idle_timeout.
    await _wait_done(task)

    assert vc.disconnect.await_count >= 1, \
        "after the panel is gone the watchdog must disconnect"


async def test_short_idle_timeout_disconnects_quickly():
    """With idle_timeout≈1s the bot must disconnect promptly once nothing
    keeps it (no playback, no pause, no panel). Loose timing: well under
    a couple of seconds, real-clock based."""
    import idleWatchdog
    import time

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    started_at = time.monotonic()
    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=1.0, poll_interval=0.1,
    )
    await _wait_done(task, timeout=3.0)
    elapsed = time.monotonic() - started_at

    assert vc.disconnect.await_count >= 1
    assert elapsed < 2.5, \
        f"watchdog should fire within ~2s of going idle, took {elapsed:.2f}s"


async def test_register_unregister_panel_counter_handles_overlap():
    """Two panels open simultaneously must both have to expire before the
    guild is considered panel-free. Otherwise a quick second /soundpad
    closing would tear down voice under the first panel."""
    import soundpadCommand

    assert not soundpadCommand.has_active_panel(100)
    soundpadCommand._register_panel(100)
    soundpadCommand._register_panel(100)
    assert soundpadCommand.has_active_panel(100)

    soundpadCommand._unregister_panel(100)
    assert soundpadCommand.has_active_panel(100), \
        "guild still has one live panel, must stay 'active'"

    soundpadCommand._unregister_panel(100)
    assert not soundpadCommand.has_active_panel(100)

    # Idempotent past zero — extra unregister doesn't blow up nor underflow.
    soundpadCommand._unregister_panel(100)
    assert not soundpadCommand.has_active_panel(100)
