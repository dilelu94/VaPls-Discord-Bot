"""Behavior: /indio keeps a per-guild conversation memory. Each exchange is
stored, fed back on the next call, isolated per guild, reset on `nuevo=True`,
evicted (short-term) after the TTL while long-term notes survive, and persisted
to disk. We keep histories below the compression threshold so no background
distillation task is spawned during these tests."""
import os
import time

import pytest

from geminiClient import GeminiError
from geminiCommand import indioLogic

KEY = "guild-100"


def history(gc, key=KEY):
    return gc._indio_history.get(key, [])


def texts(turns):
    return [p["text"] for t in turns for p in t["parts"]]


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
