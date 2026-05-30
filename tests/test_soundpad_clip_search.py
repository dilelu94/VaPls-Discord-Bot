"""Behavior tests for the programmatic clip-search and playback entrypoint.

These pin the contract callers rely on:
- find a clip by fuzzy name match,
- join a voice channel (auto-pick when none is supplied),
- play once and disconnect when the call had to connect itself.
"""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from soundpadCommand import find_best_match, iter_clips, play_clip_by_query, soundpadLogic


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"")


@pytest.fixture
def soundpad_dir(tmp_path, monkeypatch):
    """Realistic soundpad layout with a few categories and nested clips."""
    root = tmp_path / "audio_output"
    _touch(str(root / "Juji" / "la-concha-de-tu-madre-bob-esponja_to_Juji.mp3"))
    _touch(str(root / "Juji" / "victor_le_dice_a_joel_to_Juji.mp3"))
    _touch(str(root / "Mila" / "hola_che.opus"))
    _touch(str(root / "Audios" / "Quandale Dingle" / "quandale.mp3"))
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(root), raising=False)
    return str(root)


class _FakeVoiceChannel:
    """Stand-in for ``discord.VoiceChannel`` good enough for our tests."""
    def __init__(self, channel_id: int, member_count: int = 0):
        self.id = channel_id
        # Non-bot members; len() is what _pick_populated_voice_channel reads.
        self.members = [SimpleNamespace(bot=False) for _ in range(member_count)]
        self.connected_vc = None  # set by .connect()

    async def connect(self, *, reconnect=True, timeout=10.0):
        self.connected_vc = _FakeVoiceClient(self)
        return self.connected_vc


class _FakeVoiceClient:
    """Stand-in for ``discord.VoiceClient``.

    ``play`` records the source and immediately fires the ``after`` callback so
    the awaited ``done`` event resolves without real audio.
    """
    def __init__(self, channel: _FakeVoiceChannel):
        self.channel = channel
        self.guild = None
        self.played = None
        self._connected = True
        self._playing = False
        self.disconnected = False

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._playing

    def stop(self):
        self._playing = False

    def play(self, source, after=None):
        self.played = source
        self._playing = True
        if after is not None:
            after(None)

    async def disconnect(self, *, force=False):
        self._connected = False
        self.disconnected = True

    async def move_to(self, channel):
        self.channel = channel


@pytest.fixture(autouse=True)
def stub_ffmpeg(monkeypatch):
    """Replace FFmpegOpusAudio with a sentinel that remembers the path."""
    import discord
    factory = MagicMock(side_effect=lambda path, **kwargs: SimpleNamespace(path=path, kwargs=kwargs))
    monkeypatch.setattr(discord, "FFmpegOpusAudio", factory)
    return factory


def _make_guild(channels):
    guild = SimpleNamespace()
    guild.voice_channels = channels
    return guild


def _make_bot(voice_clients=()):
    return SimpleNamespace(voice_clients=list(voice_clients))


# --------------------------------------------------------------------------
# find_best_match / iter_clips
# --------------------------------------------------------------------------
def test_iter_clips_walks_categories_and_subfolders(soundpad_dir):
    found = {os.path.basename(path) for path, _ in iter_clips(soundpad_dir)}
    assert "la-concha-de-tu-madre-bob-esponja_to_Juji.mp3" in found
    assert "quandale.mp3" in found
    assert "hola_che.opus" in found


def test_find_best_match_picks_clip_with_similar_name(soundpad_dir):
    path = find_best_match("bob esponja", soundpad_dir)
    assert path is not None
    assert os.path.basename(path) == "la-concha-de-tu-madre-bob-esponja_to_Juji.mp3"


def test_find_best_match_is_case_and_separator_insensitive(soundpad_dir):
    # underscores in clip vs spaces in query, mixed case
    path = find_best_match("HOLA CHE", soundpad_dir)
    assert path is not None
    assert os.path.basename(path) == "hola_che.opus"


def test_find_best_match_returns_none_when_nothing_close_enough(soundpad_dir):
    assert find_best_match("xyzqwerty asdf nothing here", soundpad_dir, cutoff=0.8) is None


def test_find_best_match_returns_none_on_empty_dir(tmp_path):
    assert find_best_match("anything", str(tmp_path)) is None


def test_find_best_match_returns_none_on_missing_dir(tmp_path):
    assert find_best_match("anything", str(tmp_path / "does-not-exist")) is None


# --------------------------------------------------------------------------
# play_clip_by_query
# --------------------------------------------------------------------------
async def test_play_clip_by_query_plays_the_matched_clip(soundpad_dir, stub_ffmpeg):
    channel = _FakeVoiceChannel(channel_id=10, member_count=3)
    bot = _make_bot()
    guild = _make_guild([channel])

    played = await play_clip_by_query(bot, guild, query="bob esponja")

    assert played is not None
    assert os.path.basename(played) == "la-concha-de-tu-madre-bob-esponja_to_Juji.mp3"
    # FFmpegOpusAudio got called with the same absolute path we returned.
    stub_ffmpeg.assert_called()
    assert stub_ffmpeg.call_args.args[0] == played


async def test_play_clip_by_query_returns_none_when_no_match(soundpad_dir, stub_ffmpeg):
    channel = _FakeVoiceChannel(channel_id=10, member_count=3)
    bot = _make_bot()
    guild = _make_guild([channel])

    played = await play_clip_by_query(
        bot, guild, query="nothing similar at all", cutoff=0.9
    )

    assert played is None
    stub_ffmpeg.assert_not_called()


async def test_play_clip_by_query_auto_picks_most_populated_channel(soundpad_dir):
    empty = _FakeVoiceChannel(channel_id=20, member_count=0)
    busy = _FakeVoiceChannel(channel_id=21, member_count=4)
    bot = _make_bot()
    guild = _make_guild([empty, busy])

    await play_clip_by_query(bot, guild, query="bob esponja")

    assert busy.connected_vc is not None, "should have joined the populated channel"
    assert empty.connected_vc is None


async def test_play_clip_by_query_uses_explicit_channel_when_passed(soundpad_dir):
    other = _FakeVoiceChannel(channel_id=20, member_count=10)
    target = _FakeVoiceChannel(channel_id=21, member_count=1)
    bot = _make_bot()
    guild = _make_guild([other, target])

    await play_clip_by_query(bot, guild, query="bob esponja", voice_channel=target)

    assert target.connected_vc is not None
    assert other.connected_vc is None


async def test_play_clip_by_query_returns_none_when_no_voice_channel_available(soundpad_dir):
    empty = _FakeVoiceChannel(channel_id=20, member_count=0)
    bot = _make_bot()
    guild = _make_guild([empty])

    played = await play_clip_by_query(bot, guild, query="bob esponja")

    assert played is None
    assert empty.connected_vc is None


async def test_play_clip_by_query_disconnects_after_playback_when_it_had_to_connect(soundpad_dir):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    bot = _make_bot()
    guild = _make_guild([channel])

    await play_clip_by_query(bot, guild, query="hola che")

    assert channel.connected_vc is not None
    assert channel.connected_vc.disconnected, "one-shot should disconnect after playback"


# --------------------------------------------------------------------------
# /soundpad slash command with optional `query`
# --------------------------------------------------------------------------
def _make_slash_ctx(channel: _FakeVoiceChannel, guild):
    """Build a fake ApplicationContext that exercises the /soundpad logic."""
    ctx = MagicMock(name="ApplicationContext")
    ctx.guild = guild
    ctx.bot = SimpleNamespace(voice_clients=[])
    ctx.author = SimpleNamespace(
        id=1,
        display_name="Tester",
        name="tester",
        voice=SimpleNamespace(channel=channel),
    )
    ctx.response = MagicMock()
    ctx.response.is_done = MagicMock(return_value=True)
    ctx.defer = AsyncMock()
    sent: list[str] = []

    async def _send(content=None, **kwargs):
        sent.append(content)

    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock(side_effect=_send)
    ctx.sent_messages = sent
    return ctx


@pytest.fixture
def no_music_playing(monkeypatch):
    """Stub playCommand.guildPlayers so the music-playing check is a no-op."""
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=False)


async def test_soundpad_slash_with_query_plays_matched_clip_and_replies(
    soundpad_dir, no_music_playing
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)

    await soundpadLogic(ctx, query="bob esponja")

    # The user got told what is being played and the bot did join the channel.
    text = "\n".join(m for m in ctx.sent_messages if m)
    assert "Reproduciendo" in text or "reproduciendo" in text.lower()
    assert "bob esponja".lower() in text.lower().replace("_", " ").replace("-", " ")
    assert channel.connected_vc is not None
    assert channel.connected_vc.played is not None


async def test_soundpad_slash_with_query_informs_user_when_no_match(
    soundpad_dir, no_music_playing
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)

    await soundpadLogic(ctx, query="zzz nothing similar zzz")

    text = "\n".join(m for m in ctx.sent_messages if m)
    assert "encontr" in text.lower()  # "No encontré"
    # No connection should have happened on a miss.
    assert channel.connected_vc is None


async def test_soundpad_slash_with_query_rejects_user_not_in_voice(
    soundpad_dir, no_music_playing
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)
    ctx.author.voice = None  # user not in any voice channel

    await soundpadLogic(ctx, query="bob esponja")

    text = "\n".join(m for m in ctx.sent_messages if m)
    assert "voz" in text.lower() or "voice" in text.lower()
    assert channel.connected_vc is None


async def test_play_clip_by_query_stays_connected_if_bot_was_already_in_voice(soundpad_dir, monkeypatch):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    existing_vc = _FakeVoiceClient(channel)
    guild = _make_guild([channel])
    existing_vc.guild = guild
    bot = _make_bot(voice_clients=[existing_vc])

    # discord.utils.get(bot.voice_clients, guild=guild) needs to find our vc.
    import discord
    monkeypatch.setattr(
        discord.utils,
        "get",
        lambda iterable, **kwargs: next(
            (vc for vc in iterable if getattr(vc, "guild", None) is kwargs.get("guild")),
            None,
        ),
    )

    played = await play_clip_by_query(bot, guild, query="hola che")

    assert played is not None
    assert existing_vc.played is not None, "should reuse the existing voice client"
    assert not existing_vc.disconnected, "should not disconnect when reusing"
