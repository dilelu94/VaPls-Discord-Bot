"""Behavior: auto-resume after a transient voice-WS drop (region change).

These tests pin a single user-facing contract: when the bot loses the voice
connection mid-song *while still being meant to be in the channel* (the
fingerprint of a Discord region change or WS reset), it reconnects on its own
and the music keeps playing roughly where it left off — without anyone having
to type ``/play`` again.

The same machinery must NOT fire for real disconnects (kicks, ``/quit``):
those go through ``on_voice_state_update`` in ``bot.py`` and call
``mark_interrupted`` directly, leaving the player parked for the next manual
``/play``.

Mocking policy follows the behavioral-testing skill: only the Discord boundary
(VoiceClient + Guild lookup + ffmpeg) is faked. The auto-resume scheduler,
mark_interrupted, resumeFromInterruption, and onSongFinished all run for real.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --------------------------------------------------------------------------
# Fakes — kept inline so the test file reads as a contract.
# --------------------------------------------------------------------------

class FakeVC:
    """Minimal VoiceClient stub. Covers play/pause/connected + channel.id."""

    def __init__(self, *, playing=True, paused=False, connected=True,
                 guild_id=100, channel_id=999):
        self._playing = playing
        self._paused = paused
        self._connected = connected
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=channel_id, name="general",
                                       guild=self.guild)
        self.disconnect = AsyncMock(side_effect=self._on_disconnect)
        self.cleanup = MagicMock()
        self.play = MagicMock(side_effect=self._on_play)
        self.pause = MagicMock()
        self.resume = MagicMock()
        self.last_audio_source = None

    def _on_disconnect(self, force=False):
        self._connected = False
        self._playing = False

    def _on_play(self, source, *args, **kwargs):
        self.last_audio_source = source
        self._playing = True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def is_connected(self):
        return self._connected


def make_channel(channel_id: int, *, connect_returns=None, connect_raises=None):
    """Build a fake voice channel whose ``connect(reconnect=True)`` returns
    ``connect_returns`` (a FakeVC) or raises ``connect_raises``."""
    channel = MagicMock()
    channel.id = channel_id

    async def _connect(*, reconnect=True):
        if connect_raises is not None:
            raise connect_raises
        return connect_returns

    channel.connect = AsyncMock(side_effect=_connect)
    return channel


def make_bot_with_channel(guild_id: int, channel):
    """Bot whose ``get_guild(guild_id)`` returns a guild with ``get_channel``
    yielding ``channel`` (or None for any other id)."""
    guild = MagicMock()
    guild.id = guild_id
    guild.get_channel = MagicMock(
        side_effect=lambda cid: channel if cid == channel.id else None
    )

    bot = MagicMock()
    bot.loop = asyncio.get_event_loop()
    bot.voice_clients = []
    bot.get_guild = MagicMock(side_effect=lambda gid: guild if gid == guild_id else None)
    return bot


@pytest.fixture
def fast_auto_resume(monkeypatch):
    """Shrink the auto-resume delay so tests don't sleep for seconds.

    Production defaults stay untouched — we only flip the class attribute
    that the loop reads each iteration.
    """
    import playCommand
    monkeypatch.setattr(playCommand.GuildPlayer, "AUTO_RESUME_DELAY_SECONDS",
                        0.01, raising=True)
    monkeypatch.setattr(playCommand.GuildPlayer, "AUTO_RESUME_ATTEMPTS",
                        3, raising=True)
    return playCommand


@pytest.fixture
def fresh_player_state(monkeypatch):
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)
    audio_factory = MagicMock(
        side_effect=lambda filepath, **kw: SimpleNamespace(filepath=filepath, **kw)
    )
    monkeypatch.setattr("discord.FFmpegOpusAudio", audio_factory)
    yield playCommand


@pytest.fixture
def downloaded_file(tmp_path, monkeypatch):
    """Pre-create the mp3 ``resumeFromInterruption`` expects so it doesn't
    fall into the yt-dlp branch."""
    import playCommand
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    real_dirname = os.path.dirname
    monkeypatch.setattr(
        playCommand.os.path, "dirname",
        lambda p: str(tmp_path) if "playCommand" in str(p) else real_dirname(p),
        raising=True,
    )
    (downloads / "video1.mp3").write_bytes(b"\x00" * 16)
    yield {"dir": downloads}


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

async def test_region_change_drops_ws_and_bot_reconnects_on_its_own(
    fresh_player_state, fast_auto_resume, downloaded_file,
):
    """The region-change happy path.

    A region change shows up as: vc.is_connected() flips to False mid-stream
    while the bot is still meant to be in the channel. onSongFinished must
    detect that, snapshot the position, and the auto-resume loop must
    reconnect and resume — no manual /play required.
    """
    playCommand = fresh_player_state

    fresh_vc = FakeVC(playing=False, paused=False, connected=True,
                      channel_id=999)
    channel = make_channel(999, connect_returns=fresh_vc)
    bot = make_bot_with_channel(100, channel)

    player = playCommand.GuildPlayer(100, bot)
    dead_vc = FakeVC(playing=False, paused=False, connected=False,
                     channel_id=999)
    player.vc = dead_vc
    player.currentSong = {"id": "video1", "title": "Queen - A"}
    player.queue = [{"id": "video2", "title": "B"}]
    player.textChannel = MagicMock(send=AsyncMock())
    player.playStartedAt = asyncio.get_event_loop().time() - 7.0

    with patch.object(player, "updateControlMessage", new=AsyncMock()), \
         patch.object(player, "startPreDownloading", new=MagicMock()):
        await player.onSongFinished(error=None)
        # Wait for the spawned auto-resume task to run.
        assert player._autoResumeTask is not None, \
            "auto-resume must be scheduled when WS dies mid-stream"
        await player._autoResumeTask

    # The bot reconnected on its own and the music kept playing.
    assert channel.connect.await_count == 1, \
        "exactly one reconnect attempt is enough for the happy path"
    assert player.vc is fresh_vc, "player now owns the freshly-connected vc"
    assert player.interrupted is False, \
        "interrupted flag cleared after successful resume"
    assert player.currentSong == {"id": "video1", "title": "Queen - A"}
    assert player.queue == [{"id": "video2", "title": "B"}]
    # FFmpeg was launched on the new vc with a seek matching the snapshot.
    assert fresh_vc.play.call_count == 1
    src = fresh_vc.last_audio_source
    before_opts = getattr(src, "before_options", "") or ""
    assert "-ss" in before_opts, \
        f"resume must FFmpeg-seek to the saved position, got: {before_opts!r}"


async def test_kick_does_not_trigger_auto_resume(
    fresh_player_state, fast_auto_resume,
):
    """The on_voice_state_update path (kick / /quit).

    When the bot is kicked from voice, the gateway tells us. ``bot.py``
    reacts by calling ``player.mark_interrupted()`` directly — NOT
    ``_scheduleAutoResume``. This test pins the contract that
    ``mark_interrupted`` alone never schedules a reconnect, so kicked bots
    don't keep crawling back into channels they were ejected from.
    """
    playCommand = fresh_player_state
    channel = make_channel(999)  # connect would succeed if called
    bot = make_bot_with_channel(100, channel)

    player = playCommand.GuildPlayer(100, bot)
    player.vc = FakeVC(connected=False, channel_id=999)
    player.currentSong = {"id": "video1", "title": "Queen - A"}
    player.playStartedAt = asyncio.get_event_loop().time() - 4.0

    # Simulate the bot.py listener's path after a kick.
    player.mark_interrupted()

    assert player.interrupted is True
    assert player._autoResumeTask is None, \
        "kick path must not spawn an auto-resume task"
    assert channel.connect.await_count == 0, \
        "no reconnect attempts after a real kick"


async def test_failing_reconnects_give_up_without_infinite_loop(
    fresh_player_state, fast_auto_resume,
):
    """If reconnect keeps failing (network still bad, channel gone, perms
    revoked), the auto-resume loop must run a bounded number of attempts and
    then leave the player parked for manual /play. No infinite retry."""
    playCommand = fresh_player_state
    channel = make_channel(999,
                           connect_raises=RuntimeError("voice gateway sad"))
    bot = make_bot_with_channel(100, channel)

    player = playCommand.GuildPlayer(100, bot)
    player.vc = FakeVC(connected=False, channel_id=999)
    player.currentSong = {"id": "video1", "title": "Queen - A"}
    player.queue = [{"id": "video2", "title": "B"}]
    player.textChannel = MagicMock(send=AsyncMock())
    player.playStartedAt = asyncio.get_event_loop().time() - 4.0

    with patch.object(player, "updateControlMessage", new=AsyncMock()):
        await player.onSongFinished(error=None)
        assert player._autoResumeTask is not None
        await player._autoResumeTask

    # Bounded retries — not infinite.
    assert channel.connect.await_count == playCommand.GuildPlayer.AUTO_RESUME_ATTEMPTS
    # The player is still parked: state preserved for the user's next /play.
    assert player.interrupted is True
    assert player.vc is None
    assert player.currentSong == {"id": "video1", "title": "Queen - A"}
    assert player.queue == [{"id": "video2", "title": "B"}]
    # Task slot was cleared so a future onSongFinished could schedule again.
    assert player._autoResumeTask is None


async def test_auto_resume_bails_out_if_user_resumed_first(
    fresh_player_state, fast_auto_resume,
):
    """Race: while the auto-resume task is sleeping between attempts, the
    user runs ``/play`` and the manual path already brought the song back.
    The auto-resume task must notice the flag flipped and exit without
    yanking the user's fresh connection out from under them.
    """
    playCommand = fresh_player_state
    channel = make_channel(999, connect_returns=FakeVC(channel_id=999))
    bot = make_bot_with_channel(100, channel)

    player = playCommand.GuildPlayer(100, bot)
    player.vc = FakeVC(connected=False, channel_id=999)
    player.currentSong = {"id": "video1", "title": "Queen - A"}
    player.textChannel = MagicMock(send=AsyncMock())
    player.playStartedAt = asyncio.get_event_loop().time() - 3.0

    with patch.object(player, "updateControlMessage", new=AsyncMock()):
        await player.onSongFinished(error=None)
        # Race: clear the interrupted flag *before* the loop wakes from sleep.
        player.interrupted = False
        await player._autoResumeTask

    assert channel.connect.await_count == 0, \
        "loop must not reconnect once another path cleared the interruption"
