"""Behavior: /play postpones joining the voice channel until the song is
ready to play.

The bug we're pinning here: if the bot joined voice *before* downloading, it
would sit silent in the channel while yt-dlp ran (5-10s). The idle watchdog
would notice the silence and disconnect us before the first note ever played
— and the user was left staring at "✅ Descargando..." forever.

Contract:
- When ``/play`` runs and the bot is NOT in voice, ``startPlayingCurrent``
  must not require ``vc`` up front. It downloads first, joins voice second.
- When the deferred join completes, the saved target channel (and any greeting
  trigger user) is consumed exactly once.
- If a song is already playing (vc set) the lazy-connect branch is a no-op —
  we don't re-join or move channels.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakeVC:
    def __init__(self, *, guild_id=100):
        self._playing = False
        self._paused = False
        self._connected = True
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=999, name="general", guild=self.guild)
        self.disconnect = AsyncMock()
        self.cleanup = MagicMock()
        self.play = MagicMock(side_effect=self._on_play)
        self.pause = MagicMock()
        self.resume = MagicMock()
        self.last_audio_source = None

    def _on_play(self, source, *args, **kwargs):
        self.last_audio_source = source
        self._playing = True

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
    """Each test gets a fresh playCommand.guildPlayers dict and a fake
    FFmpegOpusAudio that never spawns ffmpeg."""
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)
    audio_factory = MagicMock(side_effect=lambda filepath, **kw:
                              SimpleNamespace(filepath=filepath, **kw))
    monkeypatch.setattr("discord.FFmpegOpusAudio", audio_factory)
    yield playCommand


@pytest.fixture
def downloaded_file(tmp_path, monkeypatch):
    """Pre-create a fake downloaded mp3 so startPlayingCurrent skips the yt-dlp
    branch and goes straight to playback (after the deferred connect)."""
    import playCommand
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    real_dirname = os.path.dirname
    monkeypatch.setattr(
        playCommand.os.path, "dirname",
        lambda p: str(tmp_path) if "playCommand" in str(p) else real_dirname(p),
        raising=True,
    )
    fpath = downloads / "video1.mp3"
    fpath.write_bytes(b"\x00" * 16)
    yield {"dir": downloads, "path": fpath}


async def test_deferred_connect_happens_after_download(
    fresh_player_state, downloaded_file,
):
    """When the player has a pending voice channel but no vc, startPlayingCurrent
    must connect to the pending channel and then play — not return early."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.currentSong = {"id": "video1", "title": "Bohemian Rhapsody"}
    player.textChannel = MagicMock(send=AsyncMock())

    fresh_vc = FakeVC()
    pending_channel = SimpleNamespace(
        id=4242,
        connect=AsyncMock(return_value=fresh_vc),
    )
    player.pendingVoiceChannel = pending_channel
    player.pendingTriggerUserId = 777

    with patch.object(player, "updateControlMessage", new=AsyncMock()), \
         patch.object(player, "startPreDownloading", new=MagicMock()), \
         patch("playCommand.set_pending_trigger") as set_trigger:
        await player.startPlayingCurrent()

    # The deferred connect ran exactly once with reconnect=True.
    pending_channel.connect.assert_awaited_once_with(reconnect=True)
    # Greeting trigger was wired up with the saved user id, so the join
    # produces the welcome sound just like an immediate connect would.
    set_trigger.assert_called_once_with(4242, 777)
    # Pending state consumed.
    assert player.pendingVoiceChannel is None
    assert player.pendingTriggerUserId is None
    # The player owns the fresh vc and started playback on it.
    assert player.vc is fresh_vc
    assert fresh_vc.play.call_count == 1


async def test_existing_vc_is_not_re_joined(
    fresh_player_state, downloaded_file,
):
    """The lazy-connect branch must be a no-op when vc is already set: don't
    touch any pending channel that may have been left around, don't try to
    move channels."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.currentSong = {"id": "video1", "title": "B"}
    player.textChannel = MagicMock(send=AsyncMock())

    existing_vc = FakeVC()
    player.vc = existing_vc
    # Stale pending leftover — must NOT be acted on while vc is live.
    stray_channel = SimpleNamespace(id=9999, connect=AsyncMock())
    player.pendingVoiceChannel = stray_channel

    with patch.object(player, "updateControlMessage", new=AsyncMock()), \
         patch.object(player, "startPreDownloading", new=MagicMock()), \
         patch("playCommand.set_pending_trigger") as set_trigger:
        await player.startPlayingCurrent()

    stray_channel.connect.assert_not_called()
    set_trigger.assert_not_called()
    assert player.vc is existing_vc
    assert existing_vc.play.call_count == 1


async def test_no_vc_and_no_pending_aborts_gracefully(fresh_player_state, downloaded_file):
    """Defensive guard: without vc and without a pending channel there's
    nothing to do — must not crash and must clear currentSong so the next /play
    starts clean."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.currentSong = {"id": "video1", "title": "B"}
    player.textChannel = MagicMock(send=AsyncMock())
    assert player.vc is None
    assert player.pendingVoiceChannel is None

    with patch.object(player, "updateControlMessage", new=AsyncMock()), \
         patch.object(player, "startPreDownloading", new=MagicMock()):
        await player.startPlayingCurrent()

    assert player.currentSong is None
    assert player.vc is None


async def test_cancel_download_clears_pending_channel(fresh_player_state):
    """Cancel during the deferred-download phase must wipe the pending channel
    too, so a follow-up /play starts from a clean slate."""
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, make_bot())
    player.currentSong = {"id": "v1", "title": "A"}
    player.queue = [{"id": "v2", "title": "B"}]
    player.pendingVoiceChannel = SimpleNamespace(id=4242, connect=AsyncMock())
    player.pendingTriggerUserId = 777

    interaction = MagicMock()
    interaction.edit_original_response = AsyncMock()

    await player.cancelDownload("v1", "A", interaction)

    assert player.currentSong is None
    assert player.queue == []
    assert player.pendingVoiceChannel is None
    assert player.pendingTriggerUserId is None
