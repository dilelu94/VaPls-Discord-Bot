"""Behavior: when the bot is dropped from voice by the idle watchdog, the music
control panel must not leave live "ghost buttons" the user can keep clicking.

Issue #22: on idle disconnect the bot just left, but the control message kept
its ⏮️/⏸️/⏭️/⏹️ buttons active forever. The promise we pin here:

- After an idle disconnect the same control message is edited in place into a
  disconnected state — no playback button stays clickable.
- A ▶️ Reconectar affordance is offered when there was a last song, and is
  absent when there's nothing to revive.
- Clicking Reconectar re-queues that exact song and starts playback again.

Only true boundaries are faked (Discord voice/message, FFmpeg, the yt-dlp file
on disk); our own player/watchdog code runs for real.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest


class FakeVC:
    """Minimal VoiceClient stub for the idle-check + playback surface."""

    def __init__(self, *, guild_id=100, connected=True):
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=999, name="general", guild=self.guild)
        self._connected = connected
        self.disconnect = AsyncMock(side_effect=self._on_disconnect)
        self.cleanup = MagicMock()
        self.play = MagicMock(side_effect=self._on_play)
        self.last_audio_source = None
        self._playing = False

    def _on_disconnect(self, force=False):
        self._connected = False

    def _on_play(self, source, *a, **kw):
        self.last_audio_source = source
        self._playing = True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def is_connected(self):
        return self._connected


def make_bot(vc=None, guild_id=100):
    bot = MagicMock()
    try:
        bot.loop = asyncio.get_event_loop()
    except RuntimeError:
        bot.loop = asyncio.new_event_loop()
    bot.voice_clients = [vc] if vc is not None else []
    bot.user = SimpleNamespace(id=42)
    bot.get_guild = MagicMock(return_value=SimpleNamespace(id=guild_id))
    return bot


class FakeMessage:
    """Records what the control message gets edited into."""

    def __init__(self):
        self.last_embed = None
        self.last_view = None

    async def edit(self, *, embed=None, view=None, **_):
        self.last_embed = embed
        self.last_view = view


@pytest.fixture(autouse=True)
def _reset_state():
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


def _buttons(view):
    return [c for c in view.children if isinstance(c, discord.ui.Button)]


async def test_idle_disconnect_kills_ghost_buttons():
    """After the watchdog drops the bot, the control panel is edited and no
    playback button stays clickable — the only live control is Reconnect."""
    import idleWatchdog
    from playCommand import guildPlayers, GuildPlayer

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    player = GuildPlayer(100, bot)
    player.vc = vc
    player.history = [{"id": "abc", "title": "Song A", "duration_string": "3:00"}]
    msg = FakeMessage()
    player.controlMessage = msg
    guildPlayers[100] = player

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.05, poll_interval=0.01
    )
    await _wait_done(task)

    # The same panel was edited in place (no new chatter posted elsewhere).
    assert msg.last_view is not None, "dead control panel should be edited"

    buttons = _buttons(msg.last_view)
    enabled = [b for b in buttons if not b.disabled]
    # Every still-enabled button must be the revive button — no live playback ghost.
    assert enabled, "a reconnect affordance should remain"
    assert all(b.custom_id == "btn_reconnect" for b in enabled)
    # The familiar playback controls are present but greyed out.
    assert any(b.disabled for b in buttons)

    # And the player itself was cleaned up.
    assert 100 not in guildPlayers
    assert vc.disconnect.await_count >= 1


async def test_idle_disconnect_without_last_song_has_no_reconnect():
    """Nothing to revive (no history, nothing playing) → no Reconnect button."""
    import idleWatchdog
    from playCommand import guildPlayers, GuildPlayer

    vc = FakeVC(guild_id=100)
    bot = make_bot(vc)

    player = GuildPlayer(100, bot)
    player.vc = vc
    msg = FakeMessage()
    player.controlMessage = msg
    guildPlayers[100] = player

    task = idleWatchdog.start_idle_watchdog(
        bot, 100, idle_timeout=0.05, poll_interval=0.01
    )
    await _wait_done(task)

    assert msg.last_view is not None
    buttons = _buttons(msg.last_view)
    assert not any(b.custom_id == "btn_reconnect" for b in buttons), (
        "no song to revive → no reconnect button"
    )
    assert all(b.disabled for b in buttons), "every leftover button must be dead"


async def test_idle_disconnect_without_panel_stays_silent():
    """No control panel was ever shown → nothing to edit, and the bot still
    tears down cleanly (regression guard for the silent-disconnect promise)."""
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
        bot, 100, idle_timeout=0.05, poll_interval=0.01
    )
    await _wait_done(task)

    assert vc.disconnect.await_count >= 1
    assert 100 not in guildPlayers
    assert text_channel.send.await_count == 0


# --- DisconnectedControlView wiring -----------------------------------------


async def test_reconnect_button_present_with_last_song():
    from playCommand import DisconnectedControlView

    view = DisconnectedControlView(make_bot(), 100, {"id": "x", "title": "T"})
    assert any(getattr(c, "custom_id", None) == "btn_reconnect" for c in view.children)


async def test_reconnect_button_absent_without_song():
    from playCommand import DisconnectedControlView

    view = DisconnectedControlView(make_bot(), 100, None)
    assert not any(
        getattr(c, "custom_id", None) == "btn_reconnect" for c in view.children
    )


# --- reconnectLastSong --------------------------------------------------------


async def test_reconnect_fails_without_song():
    from playCommand import reconnectLastSong

    ok, _ = await reconnectLastSong(make_bot(), 100, None)
    assert ok is False


async def test_reconnect_fails_when_nobody_in_voice(monkeypatch):
    import playCommand
    from playCommand import reconnectLastSong

    bot = MagicMock()
    bot.user = SimpleNamespace(id=42)
    bot.get_guild.return_value = SimpleNamespace(
        get_channel=lambda i: None, voice_client=None
    )
    monkeypatch.setattr(playCommand, "_pick_voice_channel", lambda b, g: None)

    ok, _ = await reconnectLastSong(bot, 100, {"id": "x", "title": "T"})
    assert ok is False


async def test_reconnect_revives_the_song(monkeypatch, tmp_path):
    """Happy path: clicking Reconnect re-joins voice and plays the exact song
    that was last playing, posting a fresh panel — no yt-dlp search needed."""
    import playCommand
    from playCommand import reconnectLastSong, guildPlayers

    # Fake FFmpeg so no ffmpeg process spawns.
    monkeypatch.setattr(
        "discord.FFmpegOpusAudio",
        MagicMock(side_effect=lambda fp, **kw: SimpleNamespace(filepath=fp, **kw)),
    )

    # Pre-create the downloaded file so startPlayingCurrent skips the yt-dlp
    # branch and goes straight to playback after the deferred connect.
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "video1.mp3").write_bytes(b"\x00" * 16)
    real_dirname = os.path.dirname
    monkeypatch.setattr(
        playCommand.os.path,
        "dirname",
        lambda p: str(tmp_path) if "playCommand" in str(p) else real_dirname(p),
        raising=True,
    )
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    fresh_vc = FakeVC()
    voice_channel = discord.VoiceChannel.__new__(discord.VoiceChannel)
    object.__setattr__(voice_channel, "id", 4242)
    voice_channel.connect = AsyncMock(return_value=fresh_vc)

    text_channel = MagicMock()
    text_channel.send = AsyncMock()

    def get_channel(cid):
        if cid == 4242:
            return voice_channel
        return text_channel

    guild = SimpleNamespace(id=100, get_channel=get_channel, voice_client=None)
    bot = MagicMock()
    bot.user = SimpleNamespace(id=42)
    bot.get_guild = MagicMock(return_value=guild)

    song = {"id": "video1", "title": "Bohemian Rhapsody"}
    with (
        patch("playCommand.set_pending_trigger"),
        patch.object(playCommand.GuildPlayer, "startPreDownloading", new=MagicMock()),
    ):
        ok, title = await reconnectLastSong(
            bot,
            100,
            song,
            voice_channel_id=4242,
            requester=SimpleNamespace(id=7),
        )

    assert ok is True
    assert title == "Bohemian Rhapsody"
    # Re-joined voice and started playing the revived song.
    voice_channel.connect.assert_awaited_once()
    assert fresh_vc.play.call_count == 1
    player = playCommand.guildPlayers.get(100)
    assert player is not None and player.currentSong["id"] == "video1"


# --- InterruptedView ---------------------------------------------------------


def _player_stub(queue=None):
    """Build a minimal player stub for InterruptedView tests."""
    from types import SimpleNamespace

    return SimpleNamespace(queue=queue or [])


async def test_interrupted_view_reconnect_present_with_queue():
    """Queue has songs → reconnect button stays."""
    from playCommand import InterruptedView

    view = InterruptedView(_player_stub(queue=[{"id": "y", "title": "Y"}]))
    assert any(
        getattr(c, "custom_id", None) == "btn_int_reconnect" for c in view.children
    )


async def test_interrupted_view_reconnect_absent_without_queue():
    """No queued songs → reconnect button is dropped."""
    from playCommand import InterruptedView

    view = InterruptedView(_player_stub(queue=[]))
    assert not any(
        getattr(c, "custom_id", None) == "btn_int_reconnect" for c in view.children
    )


async def test_interrupted_view_stop_button_always_present():
    """Stop & Clear button is always in the view, regardless of queue."""
    from playCommand import InterruptedView

    view = InterruptedView(_player_stub(queue=[]))
    assert any(getattr(c, "custom_id", None) == "btn_int_stop" for c in view.children)
