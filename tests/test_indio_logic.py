"""Behavior: /indio keeps a per-guild conversation memory. Each exchange is
stored, fed back on the next call, isolated per guild, reset on `nuevo=True`,
evicted (short-term) after the TTL while long-term notes survive, and persisted
to disk. We keep histories below the compression threshold so no background
distillation task is spawned during these tests."""
import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from geminiClient import GeminiError
from geminiCommand import indioLogic

KEY = "guild-100"


def history(gc, key=KEY):
    return gc._indio_history.get(key, [])


def texts(turns):
    return [p["text"] for t in turns for p in t["parts"]]


async def _drain_pending_tasks():
    """``indioLogic`` dispatches PLAY_* actions via ``asyncio.create_task``
    (fire-and-forget). Tests need to yield long enough for those to run
    before they can assert on the mocks."""
    current = asyncio.current_task()
    for _ in range(20):
        await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_first_call_stores_exchange_and_replies(indio, ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="todo bien che"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)
    await indioLogic(ctx, "como andas", nuevo=False)

    assert "todo bien che" in "\n".join(ctx.sent_messages)
    stored = history(indio)
    assert len(stored) == 2                               # user turn + model turn
    assert any("[Mati]: como andas" in t for t in texts(stored))
    assert "todo bien che" in texts(stored)[-1]


async def test_memory_is_fed_back_on_next_call(indio, ctx_factory, patch_generate, reply_factory):
    calls = patch_generate(reply=reply_factory(text="ajá"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)
    await indioLogic(ctx, "primera", nuevo=False)
    await indioLogic(ctx, "segunda", nuevo=False)

    # The second Gemini call receives the first exchange as history.
    second_history = calls[1]["history"]
    assert len(second_history) == 2
    assert any("primera" in p["text"] for t in second_history for p in t["parts"])


async def test_per_guild_isolation(indio, ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="hola"))
    await indioLogic(ctx_factory(guild_id=100), "uno", nuevo=False)
    await indioLogic(ctx_factory(guild_id=200), "dos", nuevo=False)

    assert len(history(indio, "guild-100")) == 2
    assert len(history(indio, "guild-200")) == 2
    # Guild 100 never sees guild 200's message.
    assert all("dos" not in t for t in texts(history(indio, "guild-100")))


async def test_same_guild_shared_across_authors(indio, ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="ok"))
    await indioLogic(ctx_factory(display_name="Mati", user_id=1, guild_id=100), "hola", nuevo=False)
    await indioLogic(ctx_factory(display_name="Viny", user_id=2, guild_id=100), "buenas", nuevo=False)

    stored = texts(history(indio, "guild-100"))
    assert any("[Mati]" in t for t in stored)
    assert any("[Viny]" in t for t in stored)


async def test_nuevo_resets_history_and_long_term(indio, ctx_factory, patch_generate, reply_factory):
    calls = patch_generate(reply=reply_factory(text="arranquemos"))
    ctx = ctx_factory(guild_id=100)
    await indioLogic(ctx, "vieja charla", nuevo=False)
    indio._indio_long_term[KEY] = {"users": {"Mati": {"traits": ["fan de python"]}}}

    await indioLogic(ctx, "empecemos de cero", nuevo=True)

    # The reset call sent an empty history to Gemini...
    assert calls[1]["history"] == []
    # ...long-term was wiped...
    assert KEY not in indio._indio_long_term
    # ...and only the post-reset exchange remains.
    stored = texts(history(indio))
    assert any("empecemos de cero" in t for t in stored)
    assert all("vieja charla" not in t for t in stored)


async def test_ttl_eviction_drops_history_but_keeps_long_term(indio):
    indio._indio_history[KEY] = [{"role": "user", "parts": [{"text": "[Mati]: hola"}]}]
    indio._indio_last_seen[KEY] = time.time() - (indio._HISTORY_TTL_SEC + 60)
    indio._indio_long_term[KEY] = {"users": {"Mati": {"traits": ["fan de python"]}}}

    indio._evict_stale_indio()

    assert KEY not in indio._indio_history          # short-term gone
    assert KEY in indio._indio_long_term            # long-term survives
    assert KEY in indio._indio_last_seen            # last_seen kept as a hint


async def test_persistence_round_trip(indio, ctx_factory, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="guardado"))
    await indioLogic(ctx_factory(guild_id=100), "acordate de esto", nuevo=False)

    assert os.path.exists(indio._mem_path)
    before = list(history(indio))

    # Wipe memory and reload from disk.
    indio._indio_history.clear()
    indio._indio_last_seen.clear()
    indio._indio_long_term.clear()
    indio._load_indio_state()

    assert texts(history(indio)) == texts(before)


async def test_error_path_does_not_store_history(indio, ctx_factory, patch_generate):
    patch_generate(error=GeminiError("blocked", kind="blocked"))
    ctx = ctx_factory(guild_id=100)
    await indioLogic(ctx, "algo", nuevo=False)         # must not raise

    assert "\n".join(ctx.sent_messages).strip()        # a friendly message shown
    assert KEY not in indio._indio_history             # nothing persisted on failure


# ---------------------------------------------------------------------------
# Function calling: when Gemini emits a play_music / play_sound function call,
# the corresponding side effect runs. This is the replacement for the old
# "[PLAY_MUSIC: ...]" / "[PLAY_SOUND: ...]" marker regex.
# ---------------------------------------------------------------------------


@pytest.fixture
def disable_relay(monkeypatch):
    """Force the indio dispatch to bypass the userbot relay and call the
    fallback paths (playCommand.playFromIndio / soundpadCommand.play_clip_by_query)
    directly, so tests can intercept them with a single mock."""
    import config
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)


def _fake_search(monkeypatch, candidates):
    """Stub the yt-dlp search boundary so music tests never spawn a subprocess."""
    import playCommand
    monkeypatch.setattr(playCommand, "_yt_dlp_search",
                        AsyncMock(return_value=list(candidates)))


async def test_play_music_single_match_plays_directly(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    """One clear search hit → no question, the indio just plays it."""
    import playCommand
    play_mock = AsyncMock(return_value=(True, "Queen"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, [
        {"id": "abc123", "title": "Queen - Bohemian Rhapsody", "duration_string": "5:55"},
    ])

    patch_generate(reply=reply_factory(
        text="dale, va Queen",
        function_calls=[{"name": "play_music", "args": {"query": "Queen"}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "ponete un tema de Queen", nuevo=False)
    await _drain_pending_tasks()

    play_mock.assert_awaited_once()
    args, kwargs = play_mock.call_args
    assert args[1] == 100                          # guild_id
    assert kwargs["songs"][0]["id"] == "abc123"   # played directly, no re-search


async def test_play_music_url_plays_directly(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    """A direct URL never triggers the picker — it plays straight away."""
    import playCommand
    play_mock = AsyncMock(return_value=(True, "ok"))
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    monkeypatch.setattr(playCommand, "_yt_dlp_search", search_mock)

    url = "https://www.youtube.com/watch?v=zzz999"
    patch_generate(reply=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": url}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), f"poné {url}", nuevo=False)
    await _drain_pending_tasks()

    play_mock.assert_awaited_once()
    assert play_mock.call_args[0][2] == url
    search_mock.assert_not_awaited()      # no disambiguation search for a URL


async def test_play_music_multiple_matches_asks_instead_of_playing(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    """Several hits → the indio lists them and waits; nothing plays yet."""
    import playCommand
    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, [
        {"id": "id1", "title": "Crímenes Perfectos (Estudio)", "duration_string": "3:54"},
        {"id": "id2", "title": "Crímenes Perfectos (En vivo)", "duration_string": "4:20"},
        {"id": "id3", "title": "Crímenes Perfectos (cover)", "duration_string": "3:40"},
    ])

    patch_generate(reply=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "Crímenes Perfectos"}}],
    ))

    ctx = ctx_factory(display_name="Mati", guild_id=100)
    await indioLogic(ctx, "poné Crímenes Perfectos", nuevo=False)
    await _drain_pending_tasks()

    shown = "\n".join(m for m in ctx.sent_messages if m)
    assert "Estudio" in shown and "En vivo" in shown    # the options were listed
    play_mock.assert_not_awaited()                       # didn't play anything yet
    assert ("guild-100", "uid:1") in indio._indio_pending_choice   # keyed by user id


async def test_pending_choice_resolved_by_number_plays_it(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    """After the indio asks, the requester's "la dos" plays the 2nd option."""
    import playCommand
    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, [
        {"id": "id1", "title": "Tema A", "duration_string": "3:00"},
        {"id": "id2", "title": "Tema B", "duration_string": "4:00"},
        {"id": "id3", "title": "Tema C", "duration_string": "5:00"},
    ])

    patch_generate(reply=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    ))

    ctx = ctx_factory(display_name="Mati", guild_id=100)
    await indioLogic(ctx, "poné algo", nuevo=False)        # indio asks
    await _drain_pending_tasks()

    await indioLogic(ctx, "la dos", nuevo=False)           # requester chooses
    await _drain_pending_tasks()

    play_mock.assert_awaited_once()
    assert play_mock.call_args.kwargs["songs"][0]["id"] == "id2"   # the 2nd candidate
    assert ("guild-100", "uid:1") not in indio._indio_pending_choice   # cleared


async def test_pending_choice_cancel_does_not_play(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    import playCommand
    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, [
        {"id": "id1", "title": "Tema A", "duration_string": "3:00"},
        {"id": "id2", "title": "Tema B", "duration_string": "4:00"},
    ])

    patch_generate(reply=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    ))

    ctx = ctx_factory(display_name="Mati", guild_id=100)
    await indioLogic(ctx, "poné algo", nuevo=False)
    await _drain_pending_tasks()

    await indioLogic(ctx, "ninguna, dejá", nuevo=False)
    await _drain_pending_tasks()

    play_mock.assert_not_awaited()
    assert ("guild-100", "uid:1") not in indio._indio_pending_choice


async def test_pending_choice_only_requester_resolves(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    """Someone else answering does not consume the requester's pending choice."""
    import playCommand
    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, [
        {"id": "id1", "title": "Tema A", "duration_string": "3:00"},
        {"id": "id2", "title": "Tema B", "duration_string": "4:00"},
    ])
    patch_generate(reply=reply_factory(text="dale"))   # any normal reply for Viny

    # Mati asks → choice pending for Mati.
    ask_gen = reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    )
    import geminiClient
    monkeypatch.setattr(geminiClient, "generate", AsyncMock(return_value=ask_gen))
    await indioLogic(ctx_factory(display_name="Mati", user_id=1, guild_id=100), "poné algo", nuevo=False)
    await _drain_pending_tasks()

    # Viny (a different user id) says "la dos" — but the pending choice is
    # Mati's, keyed by Mati's user id, so it must NOT resolve for Viny.
    monkeypatch.setattr(geminiClient, "generate",
                        AsyncMock(return_value=reply_factory(text="qué onda")))
    await indioLogic(ctx_factory(display_name="Viny", user_id=2, guild_id=100), "la dos", nuevo=False)
    await _drain_pending_tasks()

    play_mock.assert_not_awaited()
    assert ("guild-100", "uid:1") in indio._indio_pending_choice   # still waiting for Mati


async def test_pending_choice_survives_unrelated_message(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    """An unrecognised follow-up must NOT discard the options — a later valid
    answer still works while the choice is within its TTL."""
    import playCommand
    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, [
        {"id": "id1", "title": "Tema A", "duration_string": "3:00"},
        {"id": "id2", "title": "Tema B", "duration_string": "4:00"},
    ])
    patch_generate(replies=[
        reply_factory(text="dale",
                      function_calls=[{"name": "play_music", "args": {"query": "algo"}}]),
        reply_factory(text="jajaj"),   # the unrelated turn's normal reply
    ])

    ctx = ctx_factory(display_name="Mati", user_id=1, guild_id=100)
    await indioLogic(ctx, "poné algo", nuevo=False)        # indio asks
    await _drain_pending_tasks()

    await indioLogic(ctx, "jaja qué capo", nuevo=False)    # unrelated chatter
    await _drain_pending_tasks()
    assert ("guild-100", "uid:1") in indio._indio_pending_choice   # still there
    play_mock.assert_not_awaited()

    await indioLogic(ctx, "la dos", nuevo=False)           # real answer still works
    await _drain_pending_tasks()
    play_mock.assert_awaited_once()
    assert play_mock.call_args.kwargs["songs"][0]["id"] == "id2"


# --- _parse_choice pure-function behavior ----------------------------------

_CANDS = [
    {"id": "id1", "title": "Crímenes Perfectos (Estudio)", "duration_string": "3:54"},
    {"id": "id2", "title": "Crímenes Perfectos (En vivo Vélez)", "duration_string": "4:20"},
    {"id": "id3", "title": "Crímenes Perfectos (cover acústico)", "duration_string": "3:40"},
]


def test_parse_choice_by_number():
    from geminiCommand import _parse_choice
    assert _parse_choice("la 2", _CANDS) == 1
    assert _parse_choice("dale la 3", _CANDS) == 2


def test_parse_choice_by_ordinal():
    from geminiCommand import _parse_choice
    assert _parse_choice("la primera", _CANDS) == 0
    assert _parse_choice("poné la segunda", _CANDS) == 1


def test_parse_choice_by_keyword():
    from geminiCommand import _parse_choice
    assert _parse_choice("la del vivo", _CANDS) == 1
    assert _parse_choice("el cover", _CANDS) == 2


def test_parse_choice_cancel():
    from geminiCommand import _parse_choice
    assert _parse_choice("ninguna", _CANDS) == "cancel"
    assert _parse_choice("no, dejá", _CANDS) == "cancel"


def test_parse_choice_unrelated_returns_none():
    from geminiCommand import _parse_choice
    assert _parse_choice("contame un chiste", _CANDS) is None
    assert _parse_choice("", _CANDS) is None


async def test_play_sound_function_call_triggers_clip(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    import soundpadCommand
    clip_mock = AsyncMock(return_value="/audio_output/milapollo.ogg")
    monkeypatch.setattr(soundpadCommand, "play_clip_by_query", clip_mock)

    patch_generate(reply=reply_factory(
        text="tomá milapollo",
        function_calls=[{"name": "play_sound", "args": {"name": "milapollo"}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "tirate un audio milapollo", nuevo=False)
    await _drain_pending_tasks()

    clip_mock.assert_awaited_once()
    _args, kwargs = clip_mock.call_args
    assert kwargs.get("query") == "milapollo"


async def test_function_call_with_empty_text_falls_back(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    import soundpadCommand
    monkeypatch.setattr(
        soundpadCommand, "play_clip_by_query",
        AsyncMock(return_value="/audio_output/x.ogg"),
    )

    # Model emits only a functionCall, no accompanying text. The Indio must
    # still post something visible to the chat so the interaction isn't blank.
    patch_generate(reply=reply_factory(
        text="",
        function_calls=[{"name": "play_sound", "args": {"name": "milapollo"}}],
    ))

    ctx = ctx_factory(guild_id=100)
    await indioLogic(ctx, "tirate milapollo", nuevo=False)
    await _drain_pending_tasks()

    # Among the messages sent, at least one carries non-empty text content
    # that isn't just the question header.
    bodies = [m for m in ctx.sent_messages if m and "preguntó" not in m]
    assert bodies, "indio should post a fallback reply when text is empty"
    assert any(b.strip() for b in bodies)


@pytest.fixture
def enable_relay(monkeypatch):
    """Configure relay URLs so the indio dispatch goes through the slash
    invocation path. Captures every relay POST for assertions."""
    import config
    monkeypatch.setattr(config, "INDIO_RELAY_URL",
                        "http://127.0.0.1:8081/say", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "secret", raising=False)
    posts: list[dict] = []

    class _Resp:
        def __init__(self, status=200):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self):
            return ""

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None, **_):
            posts.append({"url": url, "json": json, "headers": headers})
            return _Resp(status=200)

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Sess())
    return posts


async def test_play_sound_goes_through_userbot_relay(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, enable_relay):
    # When the relay is configured, the indio invokes /soundpad as a real
    # slash command via the userbot — not the direct play_clip_by_query path.
    import soundpadCommand
    direct_clip = AsyncMock(return_value="/x.ogg")
    monkeypatch.setattr(soundpadCommand, "play_clip_by_query", direct_clip)

    patch_generate(reply=reply_factory(
        text="tomá milapollo",
        function_calls=[{"name": "play_sound", "args": {"name": "milapollo"}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "tirate un audio milapollo", nuevo=False)
    await _drain_pending_tasks()

    # The HTTP request hit /invoke_soundpad with the right query.
    soundpad_posts = [p for p in enable_relay if "/invoke_soundpad" in p["url"]]
    assert soundpad_posts, "indio should POST to /invoke_soundpad when relay is enabled"
    assert soundpad_posts[-1]["json"]["query"] == "milapollo"
    # The direct fallback was NOT used.
    direct_clip.assert_not_awaited()


async def test_skip_music_calls_player_skip(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    """Pure control verbs (skip/pause/resume/stop) don't go through any
    relay — they call methods on the existing GuildPlayer directly."""
    import playCommand
    fake_player = MagicMock()
    fake_player.skipSong = AsyncMock()
    fake_player.vc = MagicMock()
    fake_player.vc.is_playing = MagicMock(return_value=True)
    fake_player.vc.is_paused = MagicMock(return_value=False)
    monkeypatch.setitem(playCommand.guildPlayers, 100, fake_player)

    patch_generate(reply=reply_factory(
        text="dale, salteo",
        function_calls=[{"name": "skip_music", "args": {}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "saltea este tema", nuevo=False)
    await _drain_pending_tasks()

    fake_player.skipSong.assert_awaited_once()


async def test_pause_music_only_pauses_when_playing(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    import playCommand
    fake_player = MagicMock()
    fake_player.togglePausePlay = AsyncMock()
    fake_player.vc = MagicMock()
    fake_player.vc.is_playing = MagicMock(return_value=True)
    fake_player.vc.is_paused = MagicMock(return_value=False)
    monkeypatch.setitem(playCommand.guildPlayers, 100, fake_player)

    patch_generate(reply=reply_factory(
        text="dale, freno",
        function_calls=[{"name": "pause_music", "args": {}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "pausá", nuevo=False)
    await _drain_pending_tasks()

    fake_player.togglePausePlay.assert_awaited_once()


async def test_pause_music_noop_when_not_playing(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    import playCommand
    fake_player = MagicMock()
    fake_player.togglePausePlay = AsyncMock()
    fake_player.vc = MagicMock()
    fake_player.vc.is_playing = MagicMock(return_value=False)
    fake_player.vc.is_paused = MagicMock(return_value=False)
    monkeypatch.setitem(playCommand.guildPlayers, 100, fake_player)

    patch_generate(reply=reply_factory(
        text="hmm, nada está sonando",
        function_calls=[{"name": "pause_music", "args": {}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "pausá", nuevo=False)
    await _drain_pending_tasks()

    fake_player.togglePausePlay.assert_not_awaited()


async def test_resume_music_only_resumes_when_paused(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    import playCommand
    fake_player = MagicMock()
    fake_player.togglePausePlay = AsyncMock()
    fake_player.vc = MagicMock()
    fake_player.vc.is_playing = MagicMock(return_value=False)
    fake_player.vc.is_paused = MagicMock(return_value=True)
    monkeypatch.setitem(playCommand.guildPlayers, 100, fake_player)

    patch_generate(reply=reply_factory(
        text="dale, va",
        function_calls=[{"name": "resume_music", "args": {}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "seguí con la música", nuevo=False)
    await _drain_pending_tasks()

    fake_player.togglePausePlay.assert_awaited_once()


async def test_stop_music_calls_stop_playback(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    import playCommand
    fake_player = MagicMock()
    fake_player.stopPlayback = AsyncMock()
    fake_player.vc = MagicMock()
    fake_player.vc.is_playing = MagicMock(return_value=True)
    fake_player.vc.is_paused = MagicMock(return_value=False)
    monkeypatch.setitem(playCommand.guildPlayers, 100, fake_player)

    patch_generate(reply=reply_factory(
        text="listo, paro",
        function_calls=[{"name": "stop_music", "args": {}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "pará la música", nuevo=False)
    await _drain_pending_tasks()

    fake_player.stopPlayback.assert_awaited_once()


async def test_control_music_with_no_active_player_is_noop(
        indio, ctx_factory, patch_generate, reply_factory, monkeypatch):
    """If no music has been queued yet, skip/pause/resume/stop should
    silently no-op instead of creating an empty player."""
    import playCommand
    # Make sure no player exists for this guild.
    playCommand.guildPlayers.pop(100, None)

    patch_generate(reply=reply_factory(
        text="hmm, no hay nada sonando",
        function_calls=[{"name": "skip_music", "args": {}}],
    ))

    # Just verify no crash and no player was implicitly created.
    await indioLogic(ctx_factory(guild_id=100), "saltea", nuevo=False)
    await _drain_pending_tasks()

    assert 100 not in playCommand.guildPlayers


async def test_unknown_function_call_is_ignored(
        indio, ctx_factory, patch_generate, reply_factory,
        monkeypatch, disable_relay):
    import playCommand
    import soundpadCommand
    play_mock = AsyncMock(return_value=(True, "ok"))
    clip_mock = AsyncMock(return_value="/x.ogg")
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    monkeypatch.setattr(soundpadCommand, "play_clip_by_query", clip_mock)

    # A garbage tool call should never dispatch an action.
    patch_generate(reply=reply_factory(
        text="todo bien che",
        function_calls=[{"name": "send_email", "args": {"to": "x"}}],
    ))

    await indioLogic(ctx_factory(guild_id=100), "qué hacés", nuevo=False)
    await _drain_pending_tasks()

    play_mock.assert_not_awaited()
    clip_mock.assert_not_awaited()
