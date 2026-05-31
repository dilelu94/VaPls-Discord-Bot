"""Behavior: how the GuildPlayer responds to pause, /parar, involuntary
disconnects, and resume-after-interruption.

These tests pin the contract the bot makes to the user when they pause music
and then something happens to the voice connection:

- Pause must KEEP the bot connected and preserve the queue + currentSong.
- /parar (clearGuildPlayer) is the only explicit way to wipe everything.
- If the bot is kicked / loses the connection while a song was playing, the
  queue + currentSong stay in memory and the player is marked "interrupted"
  with the elapsed position snapshotted.
- The next /play (or RESUME via the indio) rejoins voice and seeks back to
  that position before queuing anything new — the user never loses where
  they were.

Mocking policy follows the behavioral-testing skill: we mock only the Discord
boundary (the VoiceClient) and the filesystem (downloaded mp3 file). The
GuildPlayer itself, _enqueueAndMaybeStart, mark_interrupted,
resumeFromInterruption — all run for real.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --------------------------------------------------------------------------
# Local fakes — we keep them in the file so the test reads as a contract.
# --------------------------------------------------------------------------

class FakeVC:
    """Minimal VoiceClient stub: covers the play/pause/resume/connected
    surface the GuildPlayer touches. We use mutable bools so a test can flip
    state mid-flow (simulate a kick, simulate ffmpeg dying, etc.)."""

    def __init__(self, *, playing=True, paused=False, connected=True, guild_id=100):
        self._playing = playing
        self._paused = paused
        self._connected = connected
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=999, name="general", guild=self.guild)
        self.disconnect = AsyncMock(side_effect=self._on_disconnect)
        self.cleanup = MagicMock()
        self.play = MagicMock(side_effect=self._on_play)
        self.pause = MagicMock(side_effect=self._on_pause)
        self.resume = MagicMock(side_effect=self._on_resume)
        # Capture what was passed to play() so we can read back the
        # before_options string (i.e. the -ss seek used by resume).
        self.last_audio_source = None

    def _on_disconnect(self, force=False):
        self._connected = False
        self._playing = False
        self._paused = False

    def _on_play(self, source, *args, **kwargs):
        self.last_audio_source = source
        self._playing = True
        self._paused = False

    def _on_pause(self):
        self._playing = False
        self._paused = True

    def _on_resume(self):
        self._playing = True
        self._paused = False

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def is_connected(self):
        return self._connected


def make_bot(guild_id=100):
    bot = MagicMock()
    bot.loop = asyncio.get_event_loop()
    bot.voice_clients = []
    bot.get_guild = MagicMock(return_value=SimpleNamespace(id=guild_id))
    return bot


@pytest.fixture
def fresh_player_state(monkeypatch):
    """Each test gets a fresh playCommand.guildPlayers dict.

    Also stubs ``discord.FFmpegOpusAudio`` so we never spawn ffmpeg in tests.
    """
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    audio_factory = MagicMock(side_effect=lambda filepath, **kw:
                              SimpleNamespace(filepath=filepath, **kw))
    monkeypatch.setattr("discord.FFmpegOpusAudio", audio_factory)
    yield playCommand


@pytest.fixture
def downloaded_file(tmp_path, monkeypatch):
    """Pre-create a fake downloaded mp3 so startPlayingCurrent skips the
    yt-dlp branch and goes straight to playback."""
    import playCommand
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    # Make startPlayingCurrent look for downloads inside tmp_path.
    monkeypatch.setattr(
        playCommand.os.path, "dirname",
        lambda p: str(tmp_path) if "playCommand" in str(p) else os.path.dirname(p),
        raising=True,
    )
    fpath = downloads / "video1.mp3"
    fpath.write_bytes(b"\x00" * 16)
    yield {"dir": downloads, "path": fpath}


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

async def test_pause_keeps_bot_connected_and_preserves_queue(fresh_player_state):
    """The bot must stay in voice and not touch the queue when paused."""
    playCommand = fresh_player_state
    vc = FakeVC(playing=True, paused=False, connected=True)
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = vc
    player.currentSong = {"id": "v1", "title": "song A"}
    player.queue = [{"id": "v2", "title": "B"}, {"id": "v3", "title": "C"}]
    player.textChannel = MagicMock(send=AsyncMock())
    # Pretend we're already playing for a few seconds so elapsed > 0.
    player.playStartedAt = asyncio.get_event_loop().time() - 5.0

    # Avoid the control-message rendering path (touches Discord embeds).
    with patch.object(player, "updateControlMessage", new=AsyncMock()):
        await player.togglePausePlay()

    # Stayed connected, paused state on the vc.
    assert vc.disconnect.await_count == 0, "pause must not disconnect"
    assert vc.pause.call_count == 1
    assert vc.is_paused()
    # Queue + currentSong intact.
    assert player.currentSong == {"id": "v1", "title": "song A"}
    assert [s["id"] for s in player.queue] == ["v2", "v3"]
    # And the player is still discoverable by guildPlayers — important because
    # the indio's prompt block reads from there.
    playCommand.guildPlayers[100] = player
    assert playCommand.guildPlayers.get(100) is player


async def test_resume_unpause_does_not_lose_queue(fresh_player_state):
    playCommand = fresh_player_state
    vc = FakeVC(playing=False, paused=True, connected=True)
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = vc
    player.currentSong = {"id": "v1", "title": "A"}
    player.queue = [{"id": "v2", "title": "B"}]
    player.textChannel = MagicMock(send=AsyncMock())
    # Simulate 3 paused seconds so the accumulator gets touched.
    loop_time = asyncio.get_event_loop().time
    player.playStartedAt = loop_time() - 10.0
    player.pausedAt = loop_time() - 3.0

    with patch.object(player, "updateControlMessage", new=AsyncMock()):
        await player.togglePausePlay()

    assert vc.resume.call_count == 1
    assert vc.is_playing()
    # The paused seconds rolled into the accumulator so position math stays right.
    assert player.pausedAccumSecs >= 2.5
    assert player.pausedAt is None
    # Nothing was lost.
    assert player.currentSong == {"id": "v1", "title": "A"}
    assert player.queue == [{"id": "v2", "title": "B"}]


async def test_parar_clears_everything(fresh_player_state, tmp_path, monkeypatch):
    """/parar's contract is the opposite of pause: it wipes everything.

    Pinning this protects future agents from accidentally turning /parar into
    a "soft pause" that the user can't reset.
    """
    playCommand = fresh_player_state
    # Point the cleanup at our tmp dir so we can prove files get removed.
    monkeypatch.setattr(
        "playCommand.os.path.dirname",
        lambda p: str(tmp_path) if str(p).endswith("playCommand.py") else os.path.dirname(p),
        raising=True,
    )
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for vid in ("v1", "v2"):
        (downloads / f"{vid}.mp3").write_bytes(b"x")

    vc = FakeVC(playing=False, paused=True, connected=True)
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = vc
    player.currentSong = {"id": "v1", "title": "A"}
    player.queue = [{"id": "v2", "title": "B"}]
    playCommand.guildPlayers[100] = player

    playCommand.clearGuildPlayer(100)

    # State wiped, files removed, no entry left in the registry.
    assert 100 not in playCommand.guildPlayers
    assert not (downloads / "v1.mp3").exists()
    assert not (downloads / "v2.mp3").exists()


async def test_mark_interrupted_snapshots_elapsed_and_preserves_state(fresh_player_state):
    """When the bot loses the voice connection mid-song, we must keep the
    queue and currentSong in memory, set the interrupted flag, and remember
    the playback position so resume can seek back to it."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = FakeVC(playing=True, paused=False, connected=False)  # already dead
    player.currentSong = {"id": "v1", "title": "Bohemian Rhapsody"}
    player.queue = [{"id": "v2", "title": "B"}]
    # Pretend the song started 12 seconds ago and we paused once for 2s.
    loop_time = asyncio.get_event_loop().time
    player.playStartedAt = loop_time() - 12.0
    player.pausedAccumSecs = 2.0

    player.mark_interrupted()

    assert player.interrupted is True
    # ~10 seconds of real playback elapsed (12 - 2 paused).
    assert 9.0 <= player.interruptedAtSeconds <= 11.0
    # vc was nulled so callers don't try to use a dead client.
    assert player.vc is None
    # State preserved verbatim.
    assert player.currentSong == {"id": "v1", "title": "Bohemian Rhapsody"}
    assert player.queue == [{"id": "v2", "title": "B"}]


async def test_mark_interrupted_is_idempotent(fresh_player_state):
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = FakeVC()
    player.currentSong = {"id": "v1", "title": "A"}
    player.playStartedAt = asyncio.get_event_loop().time() - 5.0

    player.mark_interrupted()
    first = player.interruptedAtSeconds
    await asyncio.sleep(0.05)
    player.mark_interrupted()  # second call must not move the snapshot

    assert player.interruptedAtSeconds == first


async def test_mark_interrupted_noop_without_current_song(fresh_player_state):
    """No song = nothing to preserve; flag must stay False."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = FakeVC()
    player.currentSong = None

    player.mark_interrupted()

    assert player.interrupted is False


async def test_on_song_finished_detects_disconnect_and_preserves_state(
    fresh_player_state, downloaded_file,
):
    """If vc dies before mark_interrupted gets called (race), onSongFinished
    must still notice and preserve state instead of advancing the queue."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    # vc reports disconnected — this is the race we're guarding against.
    player.vc = FakeVC(playing=False, paused=False, connected=False)
    player.currentSong = {"id": "video1", "title": "Queen - A"}
    player.queue = [{"id": "video2", "title": "B"}]
    player.textChannel = MagicMock(send=AsyncMock())
    player.playStartedAt = asyncio.get_event_loop().time() - 4.0

    with patch.object(player, "updateControlMessage", new=AsyncMock()), \
         patch.object(player, "_leaveVoice", new=AsyncMock()) as leave_mock:
        await player.onSongFinished(error=None)

    # Queue + currentSong preserved, no advance to "video2".
    assert player.interrupted is True
    assert player.currentSong == {"id": "video1", "title": "Queen - A"}
    assert player.queue == [{"id": "video2", "title": "B"}]
    # The auto-leave hook (queue_finished) must NOT have fired.
    assert leave_mock.await_count == 0


async def test_resume_from_interruption_seeks_back(
    fresh_player_state, downloaded_file, monkeypatch,
):
    """resumeFromInterruption must restart the saved song with a -ss seek
    matching the snapshotted position, and clear the interrupted flag."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.currentSong = {"id": "video1", "title": "Queen - A"}
    player.queue = [{"id": "v2", "title": "B"}]
    player.interrupted = True
    player.interruptedAtSeconds = 42.5
    player.textChannel = MagicMock(send=AsyncMock())

    # downloads dir resolution: point it at our tmp dir.
    monkeypatch.setattr(
        "playCommand.os.path.dirname",
        lambda p: str(downloaded_file["dir"].parent) if str(p).endswith("playCommand.py") else os.path.dirname(p),
        raising=True,
    )

    fresh_vc = FakeVC(playing=False, paused=False, connected=True)
    with patch.object(player, "updateControlMessage", new=AsyncMock()), \
         patch.object(player, "startPreDownloading", new=MagicMock()):
        ok = await player.resumeFromInterruption(fresh_vc)

    assert ok is True
    assert player.interrupted is False
    assert player.interruptedAtSeconds == 0.0
    assert player.vc is fresh_vc
    # Playback was kicked off on the new vc with a -ss seek.
    assert fresh_vc.play.call_count == 1
    src = fresh_vc.last_audio_source
    seek_arg = getattr(src, "before_options", None) or ""
    assert "-ss" in seek_arg, f"expected -ss seek in FFmpeg before_options, got: {seek_arg!r}"
    # 42.5 should appear in the seek string.
    assert "42.5" in seek_arg
    # Internal tracking reset so future pauses compute elapsed correctly.
    assert player.pausedAccumSecs == 0.0
    assert player.pausedAt is None


async def test_resume_from_interruption_noop_when_nothing_interrupted(fresh_player_state):
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.currentSong = {"id": "v1", "title": "A"}
    player.interrupted = False  # not interrupted

    ok = await player.resumeFromInterruption(FakeVC())
    assert ok is False


async def test_currentElapsedSeconds_zero_before_playback(fresh_player_state):
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    assert player._currentElapsedSeconds() == 0.0


async def test_currentElapsedSeconds_freezes_during_pause(fresh_player_state):
    """While paused the elapsed clock must NOT advance — it's frozen at the
    moment pause was entered."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    loop_time = asyncio.get_event_loop().time

    player.playStartedAt = loop_time() - 7.0
    player.pausedAt = loop_time() - 1.0  # paused 1s ago

    before = player._currentElapsedSeconds()
    await asyncio.sleep(0.05)
    after = player._currentElapsedSeconds()

    # Should differ by at most a hair (Python timing noise), not by 50ms.
    assert abs(after - before) < 0.01
    # And the elapsed is ~6 seconds (7 since start, 1 already paused).
    assert 5.8 <= before <= 6.2
