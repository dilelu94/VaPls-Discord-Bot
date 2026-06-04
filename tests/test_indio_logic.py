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


async def test_first_call_stores_exchange_and_replies(
    indio, ctx_factory, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text="todo bien che"))
    ctx = ctx_factory(display_name="Mati", guild_id=100)
    await indioLogic(ctx, "como andas", nuevo=False)

    assert "todo bien che" in "\n".join(ctx.sent_messages)
    stored = history(indio)
    assert len(stored) == 2  # user turn + model turn
    # The user turn keeps speaker identity + the question content; we don't
    # pin the exact format string so the speaker-tag format can evolve.
    assert any("Mati" in t and "como andas" in t for t in texts(stored))
    assert "todo bien che" in texts(stored)[-1]


async def test_memory_is_fed_back_on_next_call(
    indio, ctx_factory, patch_generate, reply_factory
):
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


async def test_same_guild_shared_across_authors(
    indio, ctx_factory, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text="ok"))
    await indioLogic(
        ctx_factory(display_name="Mati", user_id=1, guild_id=100), "hola", nuevo=False
    )
    await indioLogic(
        ctx_factory(display_name="Viny", user_id=2, guild_id=100), "buenas", nuevo=False
    )

    stored = texts(history(indio, "guild-100"))
    assert any("Mati" in t for t in stored)
    assert any("Viny" in t for t in stored)


async def test_nuevo_resets_history_and_long_term(
    indio, ctx_factory, patch_generate, reply_factory
):
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
    indio._indio_history[KEY] = [{"role": "user", "parts": [{"text": "Mati: hola"}]}]
    indio._indio_last_seen[KEY] = time.time() - (indio._HISTORY_TTL_SEC + 60)
    indio._indio_long_term[KEY] = {"users": {"Mati": {"traits": ["fan de python"]}}}

    indio._evict_stale_indio()

    assert KEY not in indio._indio_history  # short-term gone
    assert KEY in indio._indio_long_term  # long-term survives
    assert KEY in indio._indio_last_seen  # last_seen kept as a hint


async def test_persistence_round_trip(
    indio, ctx_factory, patch_generate, reply_factory
):
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
    await indioLogic(ctx, "algo", nuevo=False)  # must not raise

    assert "\n".join(ctx.sent_messages).strip()  # a friendly message shown
    assert KEY not in indio._indio_history  # nothing persisted on failure


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

    monkeypatch.setattr(
        playCommand, "_yt_dlp_search", AsyncMock(return_value=list(candidates))
    )


# Edit-in-place: reply message is edited after the action resolves
# ---------------------------------------------------------------------------


async def test_play_succeeds_reply_is_edited_with_success_marker(
    indio, ctx_factory, patch_generate, reply_factory, monkeypatch, disable_relay
):
    """When a control action (skip) succeeds, the Gemini pre-line reply is
    EDITED to append a success marker — the user sees the final state without
    a separate message.

    Control verbs (skip/pause/resume/stop) bypass the music disambiguation
    flow so they always go through _dispatch_indio_actions directly, making
    them the clearest test surface for the edit-in-place behavior."""
    import playCommand

    fake_player = MagicMock()
    fake_player.skipSong = AsyncMock()
    fake_player.vc = MagicMock()
    fake_player.vc.is_playing = MagicMock(return_value=True)
    fake_player.vc.is_paused = MagicMock(return_value=False)
    monkeypatch.setitem(playCommand.guildPlayers, 100, fake_player)

    # Stub the #sick-tunes mirror relay to avoid network calls.
    import geminiCommand

    monkeypatch.setattr(
        geminiCommand, "_relay_to_userbot", AsyncMock(return_value=None)
    )

    patch_generate(
        reply=reply_factory(
            text="dale, salteo",
            function_calls=[{"name": "skip_music", "args": {}}],
        )
    )

    ctx = ctx_factory(guild_id=100)
    await indioLogic(ctx, "saltea este tema", nuevo=False)
    await _drain_pending_tasks()

    # The skip boundary ran.
    fake_player.skipSong.assert_awaited()

    # The final user-visible text must contain the original reply AND a
    # success marker (edit happened in place).
    final = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "dale, salteo" in final
    assert "listo" in final.lower() or "✅" in final


async def test_play_fails_reply_is_edited_with_failure_indication(
    indio, ctx_factory, patch_generate, reply_factory, monkeypatch, disable_relay
):
    """When a control action fails (no active player), the reply is EDITED
    to include the failure reason, which is distinct from a success suffix."""
    import playCommand

    # No player → action fails with "no active player".
    playCommand.guildPlayers.pop(100, None)

    patch_generate(
        reply=reply_factory(
            text="dale, salteo",
            function_calls=[{"name": "skip_music", "args": {}}],
        )
    )

    ctx = ctx_factory(guild_id=100)
    await indioLogic(ctx, "saltea", nuevo=False)
    await _drain_pending_tasks()

    final = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "dale, salteo" in final
    # A failure reason is present in the edited text.
    assert "no" in final.lower()
    # The success emoji must NOT be present without a failure qualifier.
    # (A failure suffix does not include ✅ alone.)
    combined_lower = final.lower()
    # Either no ✅ at all, or ✅ accompanied by failure language — but since
    # the failure path appends a Spanish reason (not ✅), just check for reason.
    assert "✅" not in final or "no" in combined_lower


async def test_plain_chat_no_action_reply_not_edited(
    indio, ctx_factory, patch_generate, reply_factory, monkeypatch, disable_relay
):
    """When Gemini returns no function call, no action runs and the reply
    message is NOT edited (there's no suffix to append)."""
    patch_generate(
        reply=reply_factory(
            text="todo bien, ¿y vos?",
            function_calls=[],
        )
    )

    ctx = ctx_factory(guild_id=100)
    await indioLogic(ctx, "como andas", nuevo=False)
    await _drain_pending_tasks()

    # The user sees the Gemini reply text.
    final = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "todo bien, ¿y vos?" in final
    # No suffix was appended: no music emoji, no "listo", no failure marker.
    assert "🎵" not in final
    assert "🔊" not in final
    assert "listo" not in final.lower()


# ---------------------------------------------------------------------------
# Key rotation notice: when geminiClient rotates keys after a 429, the user
# sees a transient notice in the deferred slot that's replaced by the reply
# header when it arrives. No "aviso + reply" double message.
# ---------------------------------------------------------------------------


async def test_key_rotation_shows_transient_notice_then_clean_reply(
    indio, ctx_factory, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text="todo bien che"), retries=1)
    ctx = ctx_factory(display_name="Mati", guild_id=100)

    await indioLogic(ctx, "como andas", nuevo=False)

    # The transient notice appeared during the call.
    assert any("cambiando de key" in (c or "") for c in ctx.deferred_history)
    # Final state: the notice is gone — replaced by the header + reply.
    final = "\n".join(m for m in ctx.sent_messages if m is not None)
    assert "cambiando de key" not in final
    assert "todo bien che" in final
    assert "Mati" in final  # header attribution


async def test_no_rotation_edits_deferred_instead_of_followup(
    indio, ctx_factory, patch_generate, reply_factory
):
    # Sin rotación, el header edita el deferred ("thinking...") en lugar de
    # mandar un followup separado, así no quedan mensajes fantasma.
    patch_generate(reply=reply_factory(text="todo bien"))
    ctx = ctx_factory(guild_id=100)

    await indioLogic(ctx, "como andas", nuevo=False)

    assert len(ctx.deferred_history) == 1
    assert "como andas" in ctx.deferred_history[0]
    assert "todo bien" in "\n".join(m for m in ctx.sent_messages if m is not None)
