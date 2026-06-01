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
from soundpadCommand import (
    SoundpadStopView,
    _AUTOCOMPLETE_CACHE,
    find_best_match,
    iter_clips,
    play_clip_by_query,
    soundpad_query_autocomplete,
    soundpadLogic,
)


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
    sent: list[dict] = []

    async def _send(content=None, **kwargs):
        sent.append({"content": content, **kwargs})
        msg = MagicMock(name="Message")
        msg.edit = AsyncMock()
        return msg

    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock(side_effect=_send)
    ctx.sent_messages = sent
    return ctx


@pytest.fixture
def no_music_playing(monkeypatch):
    """Stub playCommand.guildPlayers so the music-playing check is a no-op."""
    import playCommand
    monkeypatch.setattr(playCommand, "guildPlayers", {}, raising=False)


@pytest.fixture
def gemini_pool():
    """Drive the gemini key pool from tests without touching disk."""
    import geminiKeys
    snapshot = list(geminiKeys._keys)

    def _set(entries):
        geminiKeys._keys.clear()
        geminiKeys._keys.extend(entries)

    _set([])
    yield _set
    geminiKeys._keys.clear()
    geminiKeys._keys.extend(snapshot)


@pytest.fixture
def caller_has_key(gemini_pool):
    """Register a key owned by the default tester (user_id=1)."""
    gemini_pool([
        {"key": "AIza" + "x" * 35, "owner_name": "Tester",
         "owner_id": "1", "note": "", "source": "test"},
    ])
    return gemini_pool


async def test_soundpad_slash_with_query_plays_matched_clip_and_replies(
    soundpad_dir, no_music_playing, caller_has_key
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)

    await soundpadLogic(ctx, query="bob esponja")

    # The user got told what is being played and the bot did join the channel.
    text = "\n".join(m["content"] for m in ctx.sent_messages if m.get("content"))
    assert "Reproduciendo" in text or "reproduciendo" in text.lower()
    assert "bob esponja".lower() in text.lower().replace("_", " ").replace("-", " ")
    assert channel.connected_vc is not None
    assert channel.connected_vc.played is not None


async def test_soundpad_slash_with_query_informs_user_when_no_match(
    soundpad_dir, no_music_playing, caller_has_key
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)

    await soundpadLogic(ctx, query="zzz nothing similar zzz")

    text = "\n".join(m["content"] for m in ctx.sent_messages if m.get("content"))
    assert "encontr" in text.lower()  # "No encontré"
    # No connection should have happened on a miss.
    assert channel.connected_vc is None


async def test_soundpad_slash_with_query_rejects_user_not_in_voice(
    soundpad_dir, no_music_playing, caller_has_key
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)
    ctx.author.voice = None  # user not in any voice channel

    await soundpadLogic(ctx, query="bob esponja")

    text = "\n".join(m["content"] for m in ctx.sent_messages if m.get("content"))
    assert "voz" in text.lower() or "voice" in text.lower()
    assert channel.connected_vc is None


async def test_soundpad_slash_with_query_attaches_stop_button(
    soundpad_dir, no_music_playing, caller_has_key
):
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)

    await soundpadLogic(ctx, query="bob esponja")

    views = [m.get("view") for m in ctx.sent_messages if m.get("view") is not None]
    assert views, "expected a view to be attached to the playback message"
    view = views[0]
    assert isinstance(view, SoundpadStopView)
    labels = [getattr(item, "label", "") or "" for item in view.children]
    assert any("Parar" in lbl for lbl in labels), f"missing stop button, got {labels}"


# --------------------------------------------------------------------------
# Gemini-key gate
# --------------------------------------------------------------------------
def test_has_user_key_matches_only_donors_owning_the_id(gemini_pool):
    import geminiKeys
    gemini_pool([
        {"key": "AIza" + "a" * 35, "owner_name": "Miles", "owner_id": "42",
         "note": "", "source": "dm"},
        {"key": "AIza" + "b" * 35, "owner_name": "unknown", "owner_id": "",
         "note": "", "source": "env"},
    ])
    assert geminiKeys.has_user_key(42) is True
    assert geminiKeys.has_user_key("42") is True
    assert geminiKeys.has_user_key(99) is False
    assert geminiKeys.has_user_key(None) is False
    assert geminiKeys.has_user_key("") is False


def test_format_contributors_line_skips_unknown_and_counts_repeats(gemini_pool):
    import geminiKeys
    gemini_pool([
        {"key": "AIza1", "owner_name": "Miles", "owner_id": "1"},
        {"key": "AIza2", "owner_name": "Miles", "owner_id": "1"},
        {"key": "AIza3", "owner_name": "Joel", "owner_id": "2"},
        {"key": "AIza4", "owner_name": "unknown", "owner_id": ""},
    ])
    line = geminiKeys.format_contributors_line()
    assert "Miles (2)" in line
    assert "Joel" in line
    assert "unknown" not in line.lower()


def test_format_contributors_line_empty_when_no_named_donors(gemini_pool):
    import geminiKeys
    gemini_pool([{"key": "x", "owner_name": "unknown", "owner_id": ""}])
    assert geminiKeys.format_contributors_line() == ""


async def test_soundpad_blocks_user_without_key_with_helpful_message(
    soundpad_dir, no_music_playing, gemini_pool
):
    # Pool has donors but the caller is not among them.
    gemini_pool([
        {"key": "AIza" + "a" * 35, "owner_name": "Miles", "owner_id": "999",
         "note": "", "source": "dm"},
    ])
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)  # author.id == 1, no key

    await soundpadLogic(ctx, query="bob esponja")

    # Caller saw an ephemeral message that explains the situation.
    blocking = [m for m in ctx.sent_messages if m.get("ephemeral")]
    assert blocking, "expected an ephemeral rejection message"
    text = blocking[0]["content"]
    assert "API key" in text or "api key" in text.lower()
    assert config.GEMINI_KEYS_DONATION_URL in text  # how to get one
    assert "Miles" in text  # contributors list
    # And nothing actually played.
    assert channel.connected_vc is None


async def test_soundpad_blocks_panel_mode_too_when_no_key(
    soundpad_dir, no_music_playing, gemini_pool
):
    gemini_pool([])  # no donors at all
    channel = _FakeVoiceChannel(channel_id=10, member_count=2)
    guild = _make_guild([channel])
    guild.id = 999
    ctx = _make_slash_ctx(channel, guild)

    await soundpadLogic(ctx)  # no query → panel path

    blocking = [m for m in ctx.sent_messages if m.get("ephemeral")]
    assert blocking, "panel mode should be gated too"
    # No view (panel) should have been sent.
    views = [m.get("view") for m in ctx.sent_messages if m.get("view") is not None]
    assert not views


# --------------------------------------------------------------------------
# Slash-handler-level gate: must reject BEFORE defer so Discord never shows
# the "thinking…" placeholder for users without a donated key. The check is
# synchronous (in-memory) so it comfortably fits in the 3s interaction window.
# --------------------------------------------------------------------------
async def test_soundpad_slash_handler_rejects_user_without_key_before_defer(
    gemini_pool, monkeypatch
):
    import bot as bot_mod

    gemini_pool([])  # nobody has donated a key

    ctx = MagicMock(name="ApplicationContext")
    ctx.author = SimpleNamespace(id=1, display_name="Tester", name="tester")
    ctx.guild = SimpleNamespace(id=999)
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()

    # If the gate doesn't fire early, safe_defer would be called. Spy on it
    # so a regression that re-orders defer/gate is loud.
    spy_defer = AsyncMock(return_value=True)
    monkeypatch.setattr(bot_mod, "safe_defer", spy_defer)
    # Ensure soundpadLogic never gets a chance to run.
    spy_logic = AsyncMock()
    monkeypatch.setattr(bot_mod, "soundpadLogic", spy_logic)

    await bot_mod.soundpad.callback(ctx, query="bob esponja")

    # The user got an immediate ephemeral and Discord never saw a defer.
    spy_defer.assert_not_awaited()
    spy_logic.assert_not_awaited()
    ctx.respond.assert_awaited_once()
    args, kwargs = ctx.respond.call_args
    assert kwargs.get("ephemeral") is True
    text = args[0] if args else kwargs.get("content", "")
    assert config.GEMINI_KEYS_DONATION_URL in text


async def test_soundpad_slash_handler_defers_and_delegates_when_caller_has_key(
    caller_has_key, monkeypatch
):
    import bot as bot_mod

    ctx = MagicMock(name="ApplicationContext")
    ctx.author = SimpleNamespace(id=1, display_name="Tester", name="tester")
    ctx.guild = SimpleNamespace(id=999)
    ctx.respond = AsyncMock()

    spy_defer = AsyncMock(return_value=True)
    monkeypatch.setattr(bot_mod, "safe_defer", spy_defer)
    spy_logic = AsyncMock()
    monkeypatch.setattr(bot_mod, "soundpadLogic", spy_logic)

    await bot_mod.soundpad.callback(ctx, query="bob esponja")

    # Happy path: defer first (so "thinking…" appears), then hand off to the
    # logic module. No early ephemeral.
    spy_defer.assert_awaited_once()
    spy_logic.assert_awaited_once()
    ctx.respond.assert_not_awaited()


async def test_soundpad_stop_button_stops_playback_and_disables_view():
    channel = _FakeVoiceChannel(channel_id=10)
    vc = _FakeVoiceClient(channel)
    vc._playing = True
    guild = SimpleNamespace(voice_client=vc, voice_channels=[channel])

    view = SoundpadStopView(guild)
    msg = MagicMock(name="Message")
    msg.edit = AsyncMock()
    view.message = msg

    interaction = MagicMock(name="Interaction")
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()

    # Trigger the button by simulating a click through py-cord's callback path.
    button_item = view.children[0]
    await button_item.callback(interaction)

    assert not vc.is_playing(), "stop button should halt current playback"
    assert all(item.disabled for item in view.children), "items should be disabled after click"
    msg.edit.assert_awaited()


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


# --------------------------------------------------------------------------
# /soundpad query autocomplete
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_autocomplete_cache():
    """Reset the autocomplete cache between tests so fixtures don't leak."""
    _AUTOCOMPLETE_CACHE.clear()
    yield
    _AUTOCOMPLETE_CACHE.clear()


def _ac_ctx(value: str):
    """Minimal stand-in for ``discord.AutocompleteContext`` (only ``.value`` is read)."""
    return SimpleNamespace(value=value)


async def test_autocomplete_returns_suggestion_matching_partial_input(soundpad_dir):
    suggestions = await soundpad_query_autocomplete(_ac_ctx("bob"))
    # The user typed "bob"; they should see the bob-esponja clip among results.
    assert any("bob" in s.lower().replace("_", " ").replace("-", " ") for s in suggestions)


async def test_autocomplete_returns_clips_when_input_empty(soundpad_dir):
    suggestions = await soundpad_query_autocomplete(_ac_ctx(""))
    # Empty input → user is just browsing; they should see existing clips.
    assert len(suggestions) > 0
    assert len(suggestions) <= 25


async def test_autocomplete_caps_at_25_results(tmp_path, monkeypatch):
    root = tmp_path / "audio_output"
    for i in range(40):
        _touch(str(root / "Bulk" / f"clip_{i:02d}.mp3"))
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(root), raising=False)

    suggestions = await soundpad_query_autocomplete(_ac_ctx(""))
    assert len(suggestions) == 25  # Discord's hard cap on autocomplete options.


async def test_autocomplete_returns_empty_when_nothing_matches(soundpad_dir):
    suggestions = await soundpad_query_autocomplete(_ac_ctx("xyzqwertyabsolutelynothing"))
    assert suggestions == []


async def test_autocomplete_is_case_and_separator_insensitive(soundpad_dir):
    upper = await soundpad_query_autocomplete(_ac_ctx("BOB ESPONJA"))
    dashed = await soundpad_query_autocomplete(_ac_ctx("bob-esponja"))
    # Both spellings find the same clip regardless of case/separator style.
    assert upper, "uppercase input should still surface suggestions"
    assert dashed, "dashed input should still surface suggestions"


async def test_autocomplete_truncates_choices_over_100_chars(tmp_path, monkeypatch):
    # Discord rejects the whole autocomplete response (400) when any single
    # choice name exceeds 100 chars — one rogue filename must not nuke the list.
    root = tmp_path / "audio_output"
    long_stem = ("Iguana lagarto desayuna con wevo jugo de china del Bueno "
                 "con pulpa sin pulpa Que! Que! toma mango") + "_to_Juji"
    assert len(long_stem) > 100  # guard: the fixture must actually be too long
    _touch(str(root / "Juji" / f"{long_stem}.mp3"))
    _touch(str(root / "Juji" / "short_juji.mp3"))
    monkeypatch.setattr(config, "CUSTOM_AUDIO_PATH", str(root), raising=False)

    results = await soundpad_query_autocomplete(_ac_ctx("juji"))
    assert results, "long filenames should not eliminate suggestions"
    for r in results:
        assert len(r) <= 100, f"choice still over Discord's cap: {len(r)} chars"


async def test_autocomplete_picks_up_new_clip_after_filesystem_change(soundpad_dir):
    # Baseline: this clip does not exist yet.
    before = await soundpad_query_autocomplete(_ac_ctx("recienllegado"))
    assert before == []

    # lsyncd drops a new file into an existing category.
    new_clip = os.path.join(soundpad_dir, "Juji", "recienllegado_to_juji.mp3")
    _touch(new_clip)
    # Bump category mtime in case the filesystem's resolution masked the create.
    os.utime(os.path.join(soundpad_dir, "Juji"), None)

    after = await soundpad_query_autocomplete(_ac_ctx("recienllegado"))
    # Cache must invalidate so the new clip becomes visible without restart.
    assert any("recienllegado" in s.lower() for s in after)
