"""Behavioral tests for idleWatchdog.

Pins the observable promise: after N seconds of an idle voice client, the bot
disconnects; while it's playing or paused, it stays. Mocks only the discord
boundary (a fake VoiceClient + fake bot exposing ``voice_clients``).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class FakeVC:
    """Minimal stub matching discord.VoiceClient's idle-check surface."""

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
    """Build a bot stub with the single helper the watchdog actually uses."""
    bot = MagicMock()
    bot.voice_clients = [vc] if vc is not None else []
    bot.get_guild = MagicMock(return_value=SimpleNamespace(id=getattr(vc.guild, "id", 0)))
    return bot


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test gets a fresh module state — no leaked tasks between tests."""
    import idleWatchdog
    from playCommand import guildPlayers
    for task in list(idleWatchdog._watchdogs.values()):
        if not task.done():
            task.cancel()
    idleWatchdog._watchdogs.clear()
    guildPlayers.clear()
    yield
    for task in list(idleWatchdog._watchdogs.values()):
        if not task.done():
            task.cancel()
    idleWatchdog._watchdogs.clear()
    guildPlayers.clear()


async def _wait_done(task, timeout=2.0):
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        pass


async def test_disconnects_after_idle_timeout():
    import idleWatchdog

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.1, poll_interval=0.02,
    )
    await _wait_done(task)

    assert vc.disconnect.await_count >= 1, "idle vc should be disconnected"


async def test_does_not_disconnect_while_playing():
    import idleWatchdog

    vc = FakeVC(guild_id=100, playing=True)
    bot = make_bot(vc)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.1, poll_interval=0.02,
    )
    # Let the watchdog poll several times while playing.
    await asyncio.sleep(0.3)
    idleWatchdog.stop_idle_watchdog(100)
    await _wait_done(task)

    assert vc.disconnect.await_count == 0, "should never disconnect while playing"


async def test_does_not_disconnect_while_paused():
    import idleWatchdog

    vc = FakeVC(guild_id=100, paused=True)
    bot = make_bot(vc)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.1, poll_interval=0.02,
    )
    await asyncio.sleep(0.3)
    idleWatchdog.stop_idle_watchdog(100)
    await _wait_done(task)

    assert vc.disconnect.await_count == 0, "paused intent must keep the bot connected"


async def test_activity_resets_the_timer():
    """A burst of playback mid-watch keeps the bot in voice."""
    import idleWatchdog

    vc = FakeVC(guild_id=100, playing=False)
    bot = make_bot(vc)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.2, poll_interval=0.02,
    )
    # Halfway through the idle window, flip to playing.
    await asyncio.sleep(0.1)
    vc._playing = True
    await asyncio.sleep(0.3)  # well past the original idle_timeout
    assert vc.disconnect.await_count == 0, "activity should have reset the timer"

    # Now go idle again and let it expire.
    vc._playing = False
    await _wait_done(task)
    assert vc.disconnect.await_count >= 1


async def test_stop_cancels_before_disconnect():
    import idleWatchdog

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=10.0, poll_interval=0.02,
    )
    await asyncio.sleep(0.05)
    idleWatchdog.stop_idle_watchdog(100)
    await _wait_done(task)

    assert vc.disconnect.await_count == 0
    assert 100 not in idleWatchdog._watchdogs


async def test_starting_twice_replaces_the_task():
    import idleWatchdog

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    first = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=10.0, poll_interval=0.05,
    )
    second = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=10.0, poll_interval=0.05,
    )
    await asyncio.sleep(0.02)

    assert first is not second
    assert first.cancelled() or first.done(), "previous task should be retired"
    idleWatchdog.stop_idle_watchdog(100)
    await _wait_done(second)


async def test_exits_when_vc_disappears():
    """If the voice client is gone (someone else disconnected), bail out."""
    import idleWatchdog

    bot = MagicMock()
    bot.voice_clients = []  # nothing to watch
    bot.get_guild = MagicMock(return_value=SimpleNamespace(id=100))

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=10.0, poll_interval=0.02,
    )
    await _wait_done(task, timeout=1.0)
    assert task.done()


async def test_disconnect_clears_guild_player_silently():
    """End-to-end: when the watchdog drops the bot, downstream state is cleaned
    and no chatter is posted to the text channel."""
    import idleWatchdog
    from playCommand import guildPlayers, GuildPlayer

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    player = GuildPlayer(100, bot)
    player.vc = vc
    text_channel = MagicMock()
    text_channel.send = AsyncMock()
    player.textChannel = text_channel
    guildPlayers[100] = player

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.05, poll_interval=0.01,
    )
    await _wait_done(task)

    # Observable: disconnected, GuildPlayer removed, no notice posted.
    assert vc.disconnect.await_count >= 1
    assert 100 not in guildPlayers
    assert text_channel.send.await_count == 0


async def test_disconnect_without_player_still_disconnects():
    """No GuildPlayer (e.g., /speak from the HTTP API) — still tear down voice."""
    import idleWatchdog

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.05, poll_interval=0.01,
    )
    await _wait_done(task)
    assert vc.disconnect.await_count >= 1
