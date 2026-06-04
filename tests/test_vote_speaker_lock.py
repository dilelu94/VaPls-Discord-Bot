"""Behavior: while a music vote is open, the bot only listens to the user
who triggered it. The goal is to stop the indio from cascading new requests
mid-vote — every "che indio…" or "el numero 2" from anyone else just piles
more votes on top of the open one.

What we pin here:

- /play populates ``MusicVote.requester_id`` with the slash invoker.
- The indio-driven vote populates ``requester_id`` with the speaker's id.
- Voice votes are restricted to the requester (other speakers are silently
  dropped).
- /soundpad is blocked while a vote is open.
- Opening a vote fires a userbot relay call to lock the speaker; closing it
  fires a clear. The relay is fire-and-forget so failures don't break
  correctness — defence-in-depth lives in the apiServer handler.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from geminiCommand import indioLogic


_CANDS = [
    {"id": "idA", "title": "Tema A", "duration_string": "3:00"},
    {"id": "idB", "title": "Tema B", "duration_string": "4:00"},
    {"id": "idC", "title": "Tema C", "duration_string": "5:00"},
]


def _fake_search(monkeypatch, candidates):
    import playCommand
    monkeypatch.setattr(playCommand, "_yt_dlp_search",
                        AsyncMock(return_value=list(candidates)))


def _freeze_vote_timer(guild_id=100):
    import playCommand
    v = playCommand.active_votes.get(guild_id)
    if v:
        v._cancel_timers()


@pytest.fixture
def disable_relay(monkeypatch):
    """Force music dispatch to bypass the userbot relay path."""
    import config
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "", raising=False)


# ---------------------------------------------------------------------------
# requester_id is populated by both vote-opening paths.
# ---------------------------------------------------------------------------


async def test_indio_voice_request_sets_requester_id_from_speaker(
        indio, ctx_factory, reply_factory, monkeypatch, disable_relay):
    """An indio voice request opens a vote whose requester_id is the speaker."""
    import geminiClient
    import playCommand

    _fake_search(monkeypatch, _CANDS)
    monkeypatch.setattr(geminiClient, "generate", AsyncMock(return_value=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    )))

    ctx = ctx_factory(display_name="Mati", user_id=77, guild_id=100)
    await indioLogic(ctx, "poné algo", nuevo=False)
    _freeze_vote_timer(100)

    vote = playCommand.active_votes.get(100)
    assert vote is not None
    assert vote.requester_id == 77


async def test_play_slash_sets_requester_id_from_ctx_author(
        indio, monkeypatch, disable_relay):
    """The /play slash command stamps requester_id with ctx.author.id so its
    vote is locked to the user who typed the command."""
    import playCommand

    # Patch yt-dlp metadata to return >1 candidate → the picker vote path.
    class _FakeProc:
        def __init__(self, stdout):
            self._stdout = stdout
            self.returncode = 0

        async def communicate(self):
            return self._stdout, b""

    fake_lines = (
        "abc123\nTema A\n3:00\n"
        "def456\nTema B\n4:00\n"
    ).encode()

    async def _fake_subprocess(*args, **kwargs):
        return _FakeProc(fake_lines)

    monkeypatch.setattr(playCommand.asyncio, "create_subprocess_exec",
                        _fake_subprocess)
    monkeypatch.setattr(playCommand, "_should_autoplay_top",
                        lambda q, t: False)

    # Build a minimal ctx that drives playLogic past the voice-channel check.
    voice_channel = types.SimpleNamespace(id=999, name="voice")
    voice_state = types.SimpleNamespace(channel=voice_channel)
    author = types.SimpleNamespace(id=4242, voice=voice_state)
    guild = types.SimpleNamespace(id=100)
    interaction = MagicMock()
    interaction.edit_original_response = AsyncMock(return_value=types.SimpleNamespace(
        id=12345, channel=types.SimpleNamespace(id=42), add_reaction=AsyncMock()))
    ctx = MagicMock(name="ApplicationContext")
    ctx.author = author
    ctx.guild = guild
    ctx.voice_client = None
    ctx.bot = MagicMock()
    ctx.interaction = interaction
    ctx.channel = types.SimpleNamespace(id=42, send=AsyncMock())
    ctx.followup = MagicMock()
    ctx.followup.send = AsyncMock(return_value=types.SimpleNamespace(
        id=98765, channel=types.SimpleNamespace(id=42), add_reaction=AsyncMock()))

    # Stub out safe_defer / safe_respond / safeEdit and the player connect.
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "safe_defer", AsyncMock(return_value=True))
    monkeypatch.setattr(bot_mod, "safe_respond", AsyncMock())
    monkeypatch.setattr(bot_mod, "safeEdit", AsyncMock())

    player = MagicMock()
    player.textChannel = None
    player._resolveMusicChannel = MagicMock(return_value=None)
    player.interrupted = False
    player.currentSong = None
    player.pendingVoiceChannel = None
    player.pendingTriggerUserId = None
    player.addSongs = AsyncMock()
    monkeypatch.setattr(playCommand, "getGuildPlayer", lambda gid, b: player)

    await playCommand.playLogic(ctx, "tema cualquiera")
    _freeze_vote_timer(100)

    vote = playCommand.active_votes.get(100)
    assert vote is not None, "free-text search with >1 candidate should open a vote"
    assert vote.requester_id == 4242


# ---------------------------------------------------------------------------
# /soundpad is blocked while a music vote is open.
# ---------------------------------------------------------------------------


async def test_soundpad_blocked_while_music_vote_open(
        indio, ctx_factory, reply_factory, monkeypatch, disable_relay):
    """The soundpad has to wait — playing a clip on top of an in-progress
    music selection makes the bot feel hyperactive."""
    import geminiClient
    import soundpadCommand
    import geminiKeys
    import playCommand

    _fake_search(monkeypatch, _CANDS)
    monkeypatch.setattr(geminiClient, "generate", AsyncMock(return_value=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    )))
    await indioLogic(ctx_factory(display_name="Opener", user_id=1, guild_id=100),
                     "poné algo", nuevo=False)
    _freeze_vote_timer(100)
    assert playCommand.active_votes.get(100) is not None

    # User has a Gemini key (so the gate that comes before ours doesn't fire).
    monkeypatch.setattr(geminiKeys, "has_user_key", lambda uid: True)
    # We shouldn't even reach the player / clip lookup.
    play_clip = AsyncMock()
    monkeypatch.setattr(soundpadCommand, "play_clip_by_query", play_clip)

    ctx = ctx_factory(display_name="Mati", user_id=2, guild_id=100)
    # soundpadLogic calls ctx.response.is_done(); stub it.
    ctx.response = MagicMock()
    ctx.response.is_done = MagicMock(return_value=True)
    ctx.author.voice = types.SimpleNamespace(channel=MagicMock())
    ctx.bot = MagicMock()

    await soundpadCommand.soundpadLogic(ctx, query="milapollo")

    play_clip.assert_not_awaited()
    visible = "\n".join(m for m in ctx.sent_messages if m)
    assert "votaci" in visible.lower()


async def test_soundpad_works_again_after_vote_closes(
        indio, ctx_factory, reply_factory, monkeypatch, disable_relay):
    """Once the vote resolves the soundpad gate lifts."""
    import geminiClient
    import soundpadCommand
    import geminiKeys
    import playCommand

    _fake_search(monkeypatch, _CANDS)
    play_from_indio = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_from_indio)
    monkeypatch.setattr(geminiClient, "generate", AsyncMock(return_value=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    )))
    await indioLogic(ctx_factory(display_name="Opener", user_id=1, guild_id=100),
                     "poné algo", nuevo=False)
    _freeze_vote_timer(100)
    # Close the vote (no votes → falls back to candidate[0]).
    await playCommand.active_votes[100]._close()
    assert playCommand.active_votes.get(100) is None

    monkeypatch.setattr(geminiKeys, "has_user_key", lambda uid: True)
    play_clip = AsyncMock(return_value="/audio_output/x.ogg")
    monkeypatch.setattr(soundpadCommand, "play_clip_by_query", play_clip)
    # The soundpad query branch looks up the clip on disk first. Tests must
    # not depend on `audio_output/` being populated — it is gitignored and
    # empty on a fresh checkout (CI). Stub the lookup so we exercise the
    # gate-then-play path regardless of filesystem state.
    monkeypatch.setattr(soundpadCommand, "find_best_match",
                        lambda query, output_dir, cutoff=0.4: "/audio_output/x.ogg")
    # Skip the "bot already playing music" gate (it's the next one in the
    # function and not what we're testing here).
    from playCommand import guildPlayers
    guildPlayers.pop(100, None)

    ctx = ctx_factory(display_name="Mati", user_id=2, guild_id=100)
    ctx.response = MagicMock()
    ctx.response.is_done = MagicMock(return_value=True)
    ctx.author.voice = types.SimpleNamespace(channel=MagicMock())
    ctx.bot = MagicMock()

    await soundpadCommand.soundpadLogic(ctx, query="milapollo")
    play_clip.assert_awaited_once()


# ---------------------------------------------------------------------------
# Vote open/close notifies the userbot to lock / unlock voice input.
# ---------------------------------------------------------------------------


@pytest.fixture
def relay_capture(monkeypatch):
    """Capture every POST the main bot makes to the userbot relay so we can
    pin the /restrict_speaker calls (open → user_id, close → null)."""
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

        async def json(self, content_type=None):
            return {"ok": True, "message_ids": []}

        async def text(self):
            return ""

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None, **_):
            posts.append({"url": url, "json": json, "headers": headers})
            return _Resp()

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Sess())
    return posts


async def test_open_vote_posts_restrict_speaker_with_requester(
        indio, ctx_factory, reply_factory, monkeypatch, relay_capture):
    """Opening a vote tells the userbot which speaker is locked."""
    import geminiClient
    import playCommand

    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, _CANDS)
    monkeypatch.setattr(geminiClient, "generate", AsyncMock(return_value=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    )))

    await indioLogic(ctx_factory(display_name="Mati", user_id=77, guild_id=100),
                     "poné algo", nuevo=False)
    _freeze_vote_timer(100)
    # The relay POST is fire-and-forget; let it run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    restrict_calls = [p for p in relay_capture
                      if str(p["url"]).endswith("/restrict_speaker")]
    assert restrict_calls, "open should POST to /restrict_speaker"
    body = restrict_calls[-1]["json"]
    assert int(body["guild_id"]) == 100
    assert int(body["user_id"]) == 77


async def test_close_vote_posts_restrict_speaker_clear(
        indio, ctx_factory, reply_factory, monkeypatch, relay_capture):
    """Closing the vote tells the userbot to lift the speaker lock."""
    import geminiClient
    import playCommand

    play_mock = AsyncMock(return_value=(True, "x"))
    monkeypatch.setattr(playCommand, "playFromIndio", play_mock)
    _fake_search(monkeypatch, _CANDS)
    monkeypatch.setattr(geminiClient, "generate", AsyncMock(return_value=reply_factory(
        text="dale",
        function_calls=[{"name": "play_music", "args": {"query": "algo"}}],
    )))

    await indioLogic(ctx_factory(display_name="Mati", user_id=77, guild_id=100),
                     "poné algo", nuevo=False)
    _freeze_vote_timer(100)
    relay_capture.clear()  # drop the "open" call; we want the "close" one
    await playCommand.active_votes[100]._close()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    restrict_calls = [p for p in relay_capture
                      if str(p["url"]).endswith("/restrict_speaker")]
    assert restrict_calls, "close should POST to /restrict_speaker"
    body = restrict_calls[-1]["json"]
    assert int(body["guild_id"]) == 100
    assert body["user_id"] is None


# ---------------------------------------------------------------------------
# Userbot /restrict_speaker endpoint + _is_speaker_allowed gate.
#
# We can't import userbot/bot.py wholesale in the dev env (it requires
# discord.py-self + voice_recv, which only live in userbot/requirements.txt).
# Same pattern as test_userbot_relay_edit.py: extract the function bodies
# from source and exec them into a controlled namespace.
# ---------------------------------------------------------------------------

from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


_USERBOT_BOT_PATH = Path(__file__).resolve().parent.parent / "userbot" / "bot.py"


def _load_restrict_handler():
    """Return (handler, _vote_restrictions dict, _is_speaker_allowed,
    config_stub) extracted from userbot/bot.py."""
    src = _USERBOT_BOT_PATH.read_text()
    lines = src.splitlines()

    def _block(needle: str) -> str:
        start = next(i for i, l in enumerate(lines) if l.startswith(needle))
        end = next(
            i for i, l in enumerate(lines[start + 1:], start=start + 1)
            if l and not l.startswith((" ", "\t")) and l.strip() != ""
            and not l.startswith("#")
        )
        return "\n".join(lines[start:end])

    # Grab the module-level state + helper + handler.
    blocks = [
        _block("_vote_restrictions: dict[int, int]"),
        _block("def _is_speaker_allowed("),
        _block("async def _relay_restrict_speaker("),
    ]

    cfg = SimpleNamespace(RELAY_SECRET="abc")

    import logging
    ns: dict = {
        "config": cfg,
        "web": web,
        "log": logging.getLogger("test_restrict_speaker"),
        "Optional": __import__("typing").Optional,
    }
    for b in blocks:
        exec(b, ns)
    return (
        ns["_relay_restrict_speaker"],
        ns["_vote_restrictions"],
        ns["_is_speaker_allowed"],
        cfg,
    )


def _make_app(handler):
    app = web.Application()
    app.router.add_post("/restrict_speaker", handler)
    return app


async def test_userbot_relay_sets_then_clears_restriction():
    """POST with a user_id sets the lock; POST with null clears it. The lock
    state controls which speakers the sinks let through."""
    handler, restrictions, is_allowed, _cfg = _load_restrict_handler()
    restrictions.clear()

    tc = TestClient(TestServer(_make_app(handler)))
    await tc.start_server()
    try:
        resp = await tc.post(
            "/restrict_speaker",
            json={"guild_id": 100, "user_id": 77},
            headers={"X-API-Secret": "abc"},
        )
        assert resp.status == 200
        assert restrictions[100] == 77
        assert is_allowed(100, 77) is True       # the requester is heard
        assert is_allowed(100, 99) is False      # everyone else is muted

        resp = await tc.post(
            "/restrict_speaker",
            json={"guild_id": 100, "user_id": None},
            headers={"X-API-Secret": "abc"},
        )
        assert resp.status == 200
        assert 100 not in restrictions
        assert is_allowed(100, 99) is True       # gate fully open again
    finally:
        await tc.close()


async def test_userbot_relay_rejects_wrong_secret():
    handler, restrictions, _is_allowed, _cfg = _load_restrict_handler()
    restrictions.clear()

    tc = TestClient(TestServer(_make_app(handler)))
    await tc.start_server()
    try:
        resp = await tc.post(
            "/restrict_speaker",
            json={"guild_id": 100, "user_id": 77},
            headers={"X-API-Secret": "wrong"},
        )
        assert resp.status == 401
        assert 100 not in restrictions
    finally:
        await tc.close()


def test_is_speaker_allowed_without_restriction():
    """Outside an active vote, the gate is open for everyone."""
    _handler, restrictions, is_allowed, _cfg = _load_restrict_handler()
    restrictions.clear()
    assert is_allowed(100, 99) is True
    assert is_allowed(None, 99) is True
    assert is_allowed(100, None) is True
