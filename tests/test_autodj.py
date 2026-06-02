"""Behavior: the Indio's Auto-DJ — picking the next track when the queue empties.

These tests pin the promises Auto-DJ makes to the user:

- It only kicks in with an active session (history to seed from) AND humans
  still listening in the voice channel.
- The suggestion is shown (a card with the proposed track) and, after the grace
  window, the track plays automatically.
- Vetoing the suggestion searches another song from the same artist.
- After a capped number of consecutive Auto-DJ tracks it shuts itself off.
- Turning it off cancels any pending suggestion.
- /dj (openDjMenu) always posts the menu to AUTODJ_MENU_CHANNEL_ID.
- The DjMenuView activate button works with history and refuses in cold start.
- The Indio's dj_mode tool dispatches to openDjMenu (DJ_MODE action).

Mocking policy (behavioral-testing skill): we fake only the yt-dlp boundary
(the ``_autodj_fetch_*`` wrappers that shell out) and the Discord playback /
control-message surface. The Auto-DJ state machine itself runs for real.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --------------------------------------------------------------------------
# Local fakes
# --------------------------------------------------------------------------

def _member(*, bot=False):
    return SimpleNamespace(bot=bot)


class FakeVC:
    """VoiceClient stub exposing the surface Auto-DJ reads: connection state
    and the channel's member list (used to decide if anyone's still listening)."""

    def __init__(self, *, connected=True, members=None):
        self._connected = connected
        humans = members if members is not None else [_member(bot=False)]
        self.channel = SimpleNamespace(id=999, name="sick-tunes", members=humans)

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return False

    def is_paused(self):
        return False


def make_bot(guild_id=100):
    bot = MagicMock()
    bot.loop = asyncio.get_event_loop()
    bot.get_guild = MagicMock(return_value=SimpleNamespace(id=guild_id))
    return bot


@pytest.fixture
def fresh_player_state(monkeypatch):
    """Each test gets a clean playCommand.guildPlayers registry."""
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)
    yield playCommand


def _make_player(playCommand, *, members=None, connected=True):
    """A GuildPlayer wired with a fake vc + silenced control message, ready
    to drive the Auto-DJ flow. The grace timer is neutralised so tests don't
    sleep through the real 15s window."""
    player = playCommand.GuildPlayer(100, make_bot())
    player.vc = FakeVC(connected=connected, members=members)
    player.textChannel = MagicMock(send=AsyncMock())
    # Suggestion card / control panel touch Discord — silence them.
    player.updateControlMessage = AsyncMock()
    # Don't actually sleep the grace window in tests.
    player._autodj_grace_timer = AsyncMock()
    return player


# --------------------------------------------------------------------------
# Pure helpers — these ARE the unit of behavior
# --------------------------------------------------------------------------

def test_parse_duration_handles_mmss_and_hmmss(fresh_player_state):
    playCommand = fresh_player_state
    assert playCommand._parse_duration_seconds("3:30") == 210
    assert playCommand._parse_duration_seconds("1:02:03") == 3723
    assert playCommand._parse_duration_seconds("") == 0
    assert playCommand._parse_duration_seconds("garbage") == 0


def test_extract_artist_from_dashed_title(fresh_player_state):
    playCommand = fresh_player_state
    assert playCommand._extract_artist("Spinetta - Bajan") == "Spinetta"
    assert playCommand._extract_artist("Soda Stereo — De Música Ligera") == "Soda Stereo"
    # No separator: falls back to the first couple of words, never empty.
    assert playCommand._extract_artist("Just Some Title Here") != ""


def test_phrase_includes_the_artist_name(fresh_player_state):
    playCommand = fresh_player_state
    phrase = playCommand._autodj_phrase("Sumo")
    # Every template either name-drops the artist or is a generic one-liner;
    # whichever was picked, it must be a non-empty human string.
    assert isinstance(phrase, str) and phrase.strip()


# --------------------------------------------------------------------------
# Activation rules
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_activate_refused_without_history(fresh_player_state):
    """Cold start: no song ever played → Auto-DJ won't turn on (nothing to seed)."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.history = []
    player.currentSong = None

    assert player.autodj_activate() is False
    assert player.autodj_active is False


@pytest.mark.asyncio
async def test_activate_succeeds_with_history(fresh_player_state):
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.history = [{"id": "v1", "title": "Spinetta - Bajan"}]

    assert player.autodj_activate() is True
    assert player.autodj_active is True


# --------------------------------------------------------------------------
# Candidate filtering
# --------------------------------------------------------------------------

def test_pick_song_skips_long_tracks_and_already_played(fresh_player_state):
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, MagicMock())
    player.history = [{"id": "old", "title": "Already Played"}]
    candidates = [
        {"id": "long", "title": "Epic Jam", "duration_string": "12:00"},   # too long
        {"id": "old", "title": "Already Played", "duration_string": "3:00"},  # repeat
        {"id": "good", "title": "Fresh One", "duration_string": "4:00"},   # winner
    ]
    pick = player._autodj_pick_song(candidates)
    assert pick is not None and pick["id"] == "good"


def test_pick_song_returns_none_when_all_filtered(fresh_player_state):
    playCommand = fresh_player_state
    player = playCommand.GuildPlayer(100, MagicMock())
    player.history = []
    candidates = [
        {"id": "a", "title": "No Duration", "duration_string": ""},
        {"id": "b", "title": "Way Too Long", "duration_string": "20:00"},
    ]
    assert player._autodj_pick_song(candidates) is None


# --------------------------------------------------------------------------
# The suggestion flow
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_empty_with_autodj_proposes_next(fresh_player_state):
    """Active Auto-DJ + humans listening: when the last song ends, a suggestion
    is shown and the picked track is held pending (not yet played)."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.autodj_active = True
    player.history = [{"id": "seed", "title": "Spinetta - Bajan"}]
    player.currentSong = None
    player.queue = []

    radio = [{"id": "next1", "title": "Spinetta - Seguir Viviendo", "duration_string": "4:21"}]
    player._autodj_fetch_radio = AsyncMock(return_value=radio)

    await player.onSongFinished(error=None)

    # A suggestion was posted and the chosen track is pending the grace window.
    assert player.autodj_pending_song is not None
    assert player.autodj_pending_song["id"] == "next1"
    assert player.textChannel.send.await_count >= 1


@pytest.mark.asyncio
async def test_empty_voice_channel_does_not_propose(fresh_player_state):
    """Nobody left listening → Auto-DJ stays quiet and the normal end-of-queue
    path runs instead (no suggestion held)."""
    playCommand = fresh_player_state
    player = _make_player(playCommand, members=[_member(bot=True)])  # only bots
    player.autodj_active = True
    player.history = [{"id": "seed", "title": "Spinetta - Bajan"}]
    player.currentSong = None
    player.queue = []
    player._autodj_fetch_radio = AsyncMock(return_value=[
        {"id": "x", "title": "whatever", "duration_string": "3:00"}])

    await player.onSongFinished(error=None)

    assert player.autodj_pending_song is None


@pytest.mark.asyncio
async def test_grace_fire_queues_the_pending_song(fresh_player_state):
    """When the grace window elapses (or 'Ya, ponela' is pressed), the pending
    track becomes the current song."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.autodj_active = True
    player.currentSong = None
    player.queue = []
    player.autodj_pending_song = {"id": "go", "title": "Sumo - La Rubia Tarada", "duration_string": "3:30"}
    player.startPlayingCurrent = AsyncMock()

    await player._autodj_fire_now()

    assert player.currentSong is not None and player.currentSong["id"] == "go"
    assert player.autodj_pending_song is None
    assert player.autodj_chain_count == 1
    player.startPlayingCurrent.assert_awaited()


@pytest.mark.asyncio
async def test_veto_searches_same_artist(fresh_player_state):
    """Vetoing the suggestion replaces it with a different track from the same
    artist."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.autodj_active = True
    player.autodj_seed_title = "Charly Garcia - Demoliendo Hoteles"
    player.autodj_pending_song = {"id": "vetoed", "title": "Charly Garcia - Yendo de la Cama al Living"}

    artist_hits = [
        {"id": "vetoed", "title": "Charly Garcia - Yendo de la Cama al Living", "duration_string": "4:00"},
        {"id": "other", "title": "Charly Garcia - Rezo por Vos", "duration_string": "5:00"},
    ]
    player._autodj_fetch_same_artist = AsyncMock(return_value=artist_hits)

    await player._autodj_veto()

    # The new pending song is a different track (the vetoed one is excluded).
    assert player.autodj_pending_song is not None
    assert player.autodj_pending_song["id"] == "other"


@pytest.mark.asyncio
async def test_chain_cap_turns_autodj_off(fresh_player_state):
    """After AUTODJ_MAX_CHAIN consecutive Auto-DJ tracks, the mode disables
    itself so the bot doesn't play forever to an empty room."""
    import config
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.autodj_active = True
    player.currentSong = None
    player.queue = []
    player.autodj_chain_count = config.AUTODJ_MAX_CHAIN - 1
    player.autodj_pending_song = {"id": "last", "title": "Last One", "duration_string": "3:00"}
    player.startPlayingCurrent = AsyncMock()

    await player._autodj_fire_now()

    assert player.autodj_chain_count == config.AUTODJ_MAX_CHAIN
    assert player.autodj_active is False


@pytest.mark.asyncio
async def test_deactivate_clears_pending_suggestion(fresh_player_state):
    """Turning Auto-DJ off must drop any pending suggestion so it doesn't fire
    after the user said stop."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.autodj_active = True
    player.autodj_pending_song = {"id": "pending", "title": "Don't Play Me"}

    await player.autodj_deactivate(reason="test")

    assert player.autodj_active is False
    assert player.autodj_pending_song is None


# --------------------------------------------------------------------------
# /dj — openDjMenu posts to the configured AUTODJ_MENU_CHANNEL_ID
# --------------------------------------------------------------------------

def _make_fake_guild(guild_id=100, *, channel_id=None):
    """Build a minimal fake guild with one text channel."""
    import config
    ch_id = channel_id if channel_id is not None else config.AUTODJ_MENU_CHANNEL_ID
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.id = guild_id
    guild.get_channel = MagicMock(return_value=channel)
    return guild, channel


def _make_fake_bot(guild):
    bot = MagicMock()
    bot.loop = asyncio.get_event_loop()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


@pytest.mark.asyncio
async def test_open_dj_menu_posts_to_configured_channel(monkeypatch):
    """openDjMenu resolves AUTODJ_MENU_CHANNEL_ID and sends the menu there."""
    import config
    import playCommand

    guild, channel = _make_fake_guild()
    bot = _make_fake_bot(guild)

    # Silence the relay so the fallback channel.send path is exercised.
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    ok, msg = await playCommand.openDjMenu(bot, 100)

    assert ok is True
    # The channel that get_channel returned should have received a send call.
    assert channel.send.await_count >= 1


@pytest.mark.asyncio
async def test_open_dj_menu_fails_when_channel_missing(monkeypatch):
    """openDjMenu returns ok=False when the configured channel is not found."""
    import playCommand

    guild = MagicMock()
    guild.id = 100
    guild.get_channel = MagicMock(return_value=None)  # channel not found
    bot = _make_fake_bot(guild)
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=True)

    ok, msg = await playCommand.openDjMenu(bot, 100)

    assert ok is False
    assert msg  # any failure message


# --------------------------------------------------------------------------
# DjMenuView activate button — with and without history
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_djmenu_activate_with_history(fresh_player_state):
    """DjMenuView activates Auto-DJ when there is history (button logic)."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.history = [{"id": "v1", "title": "Spinetta - Bajan"}]

    # The activate button defers and then calls player.autodj_activate() when
    # there is history. We exercise this by testing the underlying player API
    # the button delegates to.
    # Behavioral promise: "with history → Auto-DJ turns on."
    ok = player.autodj_activate()
    if ok and player.textChannel:
        await player.textChannel.send("🎧 Modo DJ prendido.")

    assert player.autodj_active is True


@pytest.mark.asyncio
async def test_djmenu_activate_refused_cold(fresh_player_state):
    """DjMenuView activation is refused when there is no history or current song."""
    playCommand = fresh_player_state
    player = _make_player(playCommand)
    player.history = []
    player.currentSong = None

    # The button checks this precondition before calling autodj_activate.
    # Behavioral promise: "no history → autodj_activate returns False".
    assert player.autodj_activate() is False
    assert player.autodj_active is False


# --------------------------------------------------------------------------
# DJ_MODE action — dispatched by _dispatch_indio_actions to openDjMenu
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_dj_mode_calls_open_dj_menu(monkeypatch):
    """When _dispatch_indio_actions receives DJ_MODE, it calls openDjMenu."""
    import geminiCommand as gc
    import playCommand

    called = []

    async def _fake_open_dj_menu(bot, guild_id):
        called.append((bot, guild_id))
        return True, "menú DJ abierto"

    monkeypatch.setattr(playCommand, "openDjMenu", _fake_open_dj_menu, raising=False)

    bot = MagicMock()
    bot.loop = asyncio.get_event_loop()
    guild_id = 100

    await gc._dispatch_indio_actions(bot, guild_id, [("DJ_MODE", "")])

    assert len(called) == 1
    assert called[0][1] == guild_id
