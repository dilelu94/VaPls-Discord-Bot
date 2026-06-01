"""Behavior: /sugerencias persists a user's idea, grouping similar ideas via
Gemini, and never loses the raw text — even when Gemini is unreachable."""
import json

import pytest

import config
import suggestionsCommand as sc


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    return path


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def joined(ctx):
    return "\n".join(m for m in ctx.sent_messages if m is not None)


async def test_first_idea_creates_a_group(ctx_factory, store, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text='{"new_title":"Comando /eco","new_summary":"Repetir lo que dice el usuario"}'))
    ctx = ctx_factory(display_name="Mati", user_id=42)
    await sc.sugerenciasLogic(ctx, "agrega un comando que repita lo que digo")
    data = _read(store)
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert g["title"] == "Comando /eco"
    assert g["summary"]
    assert len(g["submissions"]) == 1
    assert g["submissions"][0]["user_id"] == "42"
    assert g["submissions"][0]["user_name"] == "Mati"
    assert "repita lo que digo" in g["submissions"][0]["text"]
    assert "Comando /eco" in joined(ctx)


async def test_similar_idea_joins_existing_group(ctx_factory, store, patch_generate, reply_factory):
    patch_generate(replies=[
        reply_factory(text='{"new_title":"Comando /eco","new_summary":"Repetir input"}'),
        reply_factory(text='{"match_id":"__PLACEHOLDER__"}'),
    ])
    ctx1 = ctx_factory(display_name="Mati", user_id=1)
    await sc.sugerenciasLogic(ctx1, "agrega un /eco")
    existing_id = _read(store)["groups"][0]["id"]

    # The second classify call needs the real existing id — re-patch with the
    # right value injected. Simpler: monkeypatch _classify directly.
    async def _fake(idea, groups):
        return sc.Classification(match_id=existing_id)
    import suggestionsCommand as scmod
    orig = scmod._classify
    scmod._classify = _fake
    try:
        ctx2 = ctx_factory(display_name="Mila", user_id=2)
        await sc.sugerenciasLogic(ctx2, "hace que repita lo que digo")
    finally:
        scmod._classify = orig

    data = _read(store)
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert len(g["submissions"]) == 2
    user_names = [s["user_name"] for s in g["submissions"]]
    assert "Mati" in user_names and "Mila" in user_names
    assert "Ya teniamos" in joined(ctx2) or "parecida" in joined(ctx2)


async def test_different_idea_creates_second_group(ctx_factory, store, patch_generate, reply_factory):
    patch_generate(replies=[
        reply_factory(text='{"new_title":"Comando /eco","new_summary":"Repetir"}'),
        reply_factory(text='{"new_title":"Mute automatico","new_summary":"Mutea al que grita"}'),
    ])
    await sc.sugerenciasLogic(ctx_factory(user_id=1), "agrega /eco")
    await sc.sugerenciasLogic(ctx_factory(user_id=2), "mutea automaticamente al que grita")
    data = _read(store)
    titles = {g["title"] for g in data["groups"]}
    assert titles == {"Comando /eco", "Mute automatico"}


async def test_gemini_failure_still_saves_raw_idea(ctx_factory, store, patch_generate):
    from geminiClient import GeminiError
    patch_generate(error=GeminiError("down", kind="timeout"))
    ctx = ctx_factory(display_name="Mati", user_id=7)
    await sc.sugerenciasLogic(ctx, "podes agregar un /clima")
    data = _read(store)
    assert len(data["groups"]) == 1
    g = data["groups"][0]
    assert g.get("unprocessed") is True
    assert g["submissions"][0]["text"] == "podes agregar un /clima"
    assert g["submissions"][0]["user_id"] == "7"
    # User still gets a confirmation, idea not lost.
    out = joined(ctx)
    assert out.strip()


async def test_malformed_classifier_response_still_saves(ctx_factory, store, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text="no soy json valido jajaj"))
    ctx = ctx_factory(user_id=9)
    await sc.sugerenciasLogic(ctx, "agrega algo cool")
    data = _read(store)
    assert len(data["groups"]) == 1
    assert data["groups"][0].get("unprocessed") is True
    assert data["groups"][0]["submissions"][0]["text"] == "agrega algo cool"


async def test_empty_idea_does_not_persist(ctx_factory, store, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text='{"new_title":"x","new_summary":"y"}'))
    ctx = ctx_factory()
    await sc.sugerenciasLogic(ctx, "   ")
    # Either file does not exist or has no groups.
    assert not store.exists() or _read(store)["groups"] == []
    out = joined(ctx)
    assert out.strip()  # user gets some kind of hint


async def test_unknown_match_id_falls_back_to_create(ctx_factory, store, patch_generate, reply_factory):
    # Classifier hallucinated a match_id that doesn't exist + provided new_title.
    patch_generate(reply=reply_factory(
        text='{"match_id":"g_doesnotexist","new_title":"Nueva idea","new_summary":"x"}',
    ))
    ctx = ctx_factory(user_id=3)
    await sc.sugerenciasLogic(ctx, "una idea")
    data = _read(store)
    assert len(data["groups"]) == 1
    assert data["groups"][0]["title"] == "Nueva idea"


async def test_very_long_idea_is_truncated_but_saved(ctx_factory, store, patch_generate, reply_factory):
    patch_generate(reply=reply_factory(text='{"new_title":"Idea larga","new_summary":"x"}'))
    long_idea = "x" * 5000
    ctx = ctx_factory(user_id=4)
    await sc.sugerenciasLogic(ctx, long_idea)
    data = _read(store)
    stored = data["groups"][0]["submissions"][0]["text"]
    assert len(stored) <= sc._MAX_IDEA_CHARS
    assert stored == "x" * sc._MAX_IDEA_CHARS
