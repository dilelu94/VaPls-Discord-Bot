"""Behavior: /sugerencias persists a user's idea only after Gemini categorizes
it, grouping similar ideas and telling the user which group it joined.
/sugerencias-ver shows the existing groups ranked by how many people asked for
each. When Gemini can't categorize, nothing is persisted and the user is asked
to retry — categorized-or-nothing."""

import json
from unittest.mock import AsyncMock

import pytest

import config
import suggestionsCommand as sc


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    monkeypatch.setattr(config, "GITHUB_TOKEN", "", raising=False)
    return path


_NOW = "2026-06-01T00:00:00Z"


def _sub(user_id="1", user_name="anon", text="idea"):
    return {"user_id": user_id, "user_name": user_name, "text": text, "at": _NOW}


def _seed(path, groups):
    """Write a suggestions.json with the given groups (filesystem boundary)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, ensure_ascii=False)


def _group(gid, title, summary, n_subs):
    return {
        "id": gid,
        "title": title,
        "summary": summary,
        "created_at": _NOW,
        "updated_at": _NOW,
        "submissions": [_sub(user_id=str(i)) for i in range(n_subs)],
    }


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def joined(ctx):
    return "\n".join(m for m in ctx.sent_messages if m is not None)


# --------------------------------------------------------------------------
# Submit: a categorized idea is persisted and the user learns where it landed.
# --------------------------------------------------------------------------
async def test_first_idea_creates_a_group(
    ctx_factory, store, patch_generate, reply_factory
):
    patch_generate(
        reply=reply_factory(
            text='{"new_title":"Comando /eco","new_summary":"Repetir lo que dice el usuario"}'
        )
    )
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


async def test_similar_idea_joins_existing_group(ctx_factory, store, monkeypatch):
    _seed(store, [_group("g_eco", "Comando /eco", "Repetir input", 1)])

    async def _fake(idea, groups):
        return sc.Classification(match_id="g_eco")

    monkeypatch.setattr(sc, "_classify", _fake)

    ctx = ctx_factory(display_name="Mila", user_id=2)
    await sc.sugerenciasLogic(ctx, "hace que repita lo que digo")

    g = _read(store)["groups"][0]
    assert len(g["submissions"]) == 2
    assert "Mila" in [s["user_name"] for s in g["submissions"]]
    # User learns it joined that specific group.
    assert "Comando /eco" in joined(ctx)


async def test_matched_reply_reports_count_of_preexisting(
    ctx_factory, store, monkeypatch
):
    # 3 people already asked for this; the 4th should be told "3 already there".
    _seed(store, [_group("g_vote", "Mejor sistema de votacion", "Votar mejor", 3)])

    async def _fake(idea, groups):
        return sc.Classification(match_id="g_vote")

    monkeypatch.setattr(sc, "_classify", _fake)

    ctx = ctx_factory(user_id=99)
    await sc.sugerenciasLogic(ctx, "mejoremos la votacion")

    out = joined(ctx)
    assert "Mejor sistema de votacion" in out
    assert "3" in out  # count of pre-existing similar suggestions
    # And it really was appended (now 4 total).
    assert len(_read(store)["groups"][0]["submissions"]) == 4


async def test_different_idea_creates_second_group(
    ctx_factory, store, patch_generate, reply_factory
):
    patch_generate(
        replies=[
            reply_factory(text='{"new_title":"Comando /eco","new_summary":"Repetir"}'),
            reply_factory(
                text='{"new_title":"Mute automatico","new_summary":"Mutea al que grita"}'
            ),
        ]
    )
    await sc.sugerenciasLogic(ctx_factory(user_id=1), "agrega /eco")
    await sc.sugerenciasLogic(
        ctx_factory(user_id=2), "mutea automaticamente al que grita"
    )
    data = _read(store)
    titles = {g["title"] for g in data["groups"]}
    assert titles == {"Comando /eco", "Mute automatico"}


# --------------------------------------------------------------------------
# Categorized-or-nothing: Gemini must succeed for anything to be saved.
# --------------------------------------------------------------------------
async def test_gemini_failure_does_not_persist_and_asks_retry(
    ctx_factory, store, patch_generate
):
    from geminiClient import GeminiError

    patch_generate(error=GeminiError("down", kind="timeout"))
    ctx = ctx_factory(display_name="Mati", user_id=7)
    await sc.sugerenciasLogic(ctx, "podes agregar un /clima")
    # Nothing persisted.
    assert not store.exists() or _read(store)["groups"] == []
    # User is told to try again (some message reaches them).
    assert joined(ctx).strip()


async def test_malformed_classifier_response_does_not_persist(
    ctx_factory, store, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text="no soy json valido jajaj"))
    ctx = ctx_factory(user_id=9)
    await sc.sugerenciasLogic(ctx, "agrega algo cool")
    assert not store.exists() or _read(store)["groups"] == []
    assert joined(ctx).strip()


async def test_empty_idea_does_not_persist(
    ctx_factory, store, patch_generate, reply_factory
):
    patch_generate(reply=reply_factory(text='{"new_title":"x","new_summary":"y"}'))
    ctx = ctx_factory()
    await sc.sugerenciasLogic(ctx, "   ")
    assert not store.exists() or _read(store)["groups"] == []
    assert joined(ctx).strip()  # user gets some kind of hint


async def test_unknown_match_id_falls_back_to_create(
    ctx_factory, store, patch_generate, reply_factory
):
    # Classifier hallucinated a match_id that doesn't exist + provided new_title.
    patch_generate(
        reply=reply_factory(
            text='{"match_id":"g_doesnotexist","new_title":"Nueva idea","new_summary":"x"}',
        )
    )
    ctx = ctx_factory(user_id=3)
    await sc.sugerenciasLogic(ctx, "una idea")
    data = _read(store)
    assert len(data["groups"]) == 1
    assert data["groups"][0]["title"] == "Nueva idea"


async def test_very_long_idea_is_truncated_but_saved(
    ctx_factory, store, patch_generate, reply_factory
):
    patch_generate(
        reply=reply_factory(text='{"new_title":"Idea larga","new_summary":"x"}')
    )
    long_idea = "x" * 5000
    ctx = ctx_factory(user_id=4)
    await sc.sugerenciasLogic(ctx, long_idea)
    data = _read(store)
    stored = data["groups"][0]["submissions"][0]["text"]
    assert len(stored) <= sc._MAX_IDEA_CHARS
    assert stored == "x" * sc._MAX_IDEA_CHARS


# --------------------------------------------------------------------------
# /sugerencias-ver: anyone can see the existing groups, busiest first.
# --------------------------------------------------------------------------
async def test_ver_lists_groups_ranked_by_demand(ctx_factory, store):
    _seed(
        store,
        [
            _group("g_poco", "Idea con poca demanda", "x", 1),
            _group("g_mucho", "Idea muy pedida", "x", 3),
            _group("g_medio", "Idea pedida a medias", "x", 2),
        ],
    )
    ctx = ctx_factory()
    await sc.sugerenciasVerLogic(ctx)
    out = joined(ctx)
    assert "Idea muy pedida" in out
    assert "Idea pedida a medias" in out
    assert "Idea con poca demanda" in out
    # Busiest group is listed before the less-popular ones.
    assert out.index("Idea muy pedida") < out.index("Idea pedida a medias")
    assert out.index("Idea pedida a medias") < out.index("Idea con poca demanda")


async def test_ver_with_no_suggestions_says_so(ctx_factory, store):
    ctx = ctx_factory()
    await sc.sugerenciasVerLogic(ctx)
    # Friendly non-empty message, no crash.
    assert joined(ctx).strip()


# --------------------------------------------------------------------------
# GitHub Issues integration
# --------------------------------------------------------------------------
async def test_new_group_creates_github_issue(
    ctx_factory, store, patch_generate, reply_factory, monkeypatch
):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    create = AsyncMock(return_value=42)
    monkeypatch.setattr(sc, "_sync_github_created", create)

    patch_generate(
        reply=reply_factory(
            text='{"new_title":"Comando /eco","new_summary":"Repetir input"}'
        )
    )
    ctx = ctx_factory(user_id=1)
    await sc.sugerenciasLogic(ctx, "agrega /eco")
    assert create.called
    g = create.call_args[0][0]
    assert g.title == "Comando /eco"


async def test_matched_suggestion_adds_comment(ctx_factory, store, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    _seed(store, [_group("g_eco", "Comando /eco", "Repetir input", 2)])

    matched_mock = AsyncMock()
    monkeypatch.setattr(sc, "_sync_github_matched", matched_mock)

    async def _fake(idea, groups):
        return sc.Classification(match_id="g_eco")

    monkeypatch.setattr(sc, "_classify", _fake)

    ctx = ctx_factory(user_id=99)
    await sc.sugerenciasLogic(ctx, "repeti lo que digo")
    assert matched_mock.called
    args = matched_mock.call_args[0]
    assert args[1].user_id == "99"
    assert args[2].match_id == "g_eco"


async def test_matched_with_update_edits_issue(ctx_factory, store, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    _seed(store, [_group("g_eco", "Comando /eco", "Repetir input", 1)])

    matched_mock = AsyncMock()
    monkeypatch.setattr(sc, "_sync_github_matched", matched_mock)

    async def _fake(idea, groups):
        return sc.Classification(
            match_id="g_eco",
            update_title="Comando /eco avanzado",
            update_summary="Repetir input con opciones de voz y texto",
        )

    monkeypatch.setattr(sc, "_classify", _fake)

    ctx = ctx_factory(user_id=7)
    await sc.sugerenciasLogic(ctx, "que repita pero con opciones")
    assert matched_mock.called
    args = matched_mock.call_args[0]
    assert args[2].update_title == "Comando /eco avanzado"
    assert args[2].update_summary is not None


async def test_no_github_token_skips_github(
    ctx_factory, store, patch_generate, reply_factory
):
    patch_generate(
        reply=reply_factory(text='{"new_title":"Comando /eco","new_summary":"Repetir"}')
    )
    ctx = ctx_factory(user_id=1)
    await sc.sugerenciasLogic(ctx, "agrega /eco")
    data = _read(store)
    g = data["groups"][0]
    assert g.get("issue_number") is None


async def test_migration_dry_run_reports_count(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(
        path,
        [
            _group("g_a", "Grupo A", "xa", 1),
            _group("g_b", "Grupo B", "xb", 2),
        ],
    )

    result = await sc.migrate_existing_suggestions(dry_run=True)
    assert "2" in result


async def test_migration_creates_issues(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(path, [_group("g_a", "Grupo A", "xa", 1)])

    create_mock = AsyncMock(return_value=100)
    monkeypatch.setattr(sc, "_sync_github_created", create_mock)

    result = await sc.migrate_existing_suggestions(dry_run=False)
    assert "Migrados" in result
    assert "1" in result

    data = _read(path)
    assert data["groups"][0].get("issue_number") == 100


# --------------------------------------------------------------------------
# sync_closed_issues: deletes groups when GitHub issue is closed.
# --------------------------------------------------------------------------
async def test_sync_closed_issues_deletes_group(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(
        path,
        [
            _group("g_a", "Grupo A", "xa", 1),
            _group("g_b", "Grupo B", "xb", 2),
        ],
    )
    data = _read(path)
    data["groups"][0]["issue_number"] = 42
    data["groups"][1]["issue_number"] = 99
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)

    import githubIssues

    monkeypatch.setattr(
        githubIssues, "list_closed_issues", AsyncMock(return_value=[42])
    )

    result = await sc.sync_closed_issues()
    assert "eliminados" in result
    assert "1" in result

    data = _read(path)
    assert len(data["groups"]) == 1
    assert data["groups"][0]["id"] == "g_b"


async def test_sync_closed_issues_multiple_deleted(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(
        path,
        [
            _group("g_a", "Grupo A", "xa", 1),
            _group("g_b", "Grupo B", "xb", 2),
            _group("g_c", "Grupo C", "xc", 3),
        ],
    )
    data = _read(path)
    data["groups"][0]["issue_number"] = 42
    data["groups"][1]["issue_number"] = 99
    data["groups"][2]["issue_number"] = 7
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)

    import githubIssues

    monkeypatch.setattr(
        githubIssues, "list_closed_issues", AsyncMock(return_value=[42, 99])
    )

    result = await sc.sync_closed_issues()
    assert "2" in result

    data = _read(path)
    assert len(data["groups"]) == 1
    assert data["groups"][0]["id"] == "g_c"


async def test_sync_closed_issues_no_match(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(path, [_group("g_a", "Grupo A", "xa", 1)])
    data = _read(path)
    data["groups"][0]["issue_number"] = 42
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)

    import githubIssues

    monkeypatch.setattr(
        githubIssues, "list_closed_issues", AsyncMock(return_value=[99])
    )

    result = await sc.sync_closed_issues()
    assert "Ningún grupo" in result

    data = _read(path)
    assert len(data["groups"]) == 1


async def test_sync_closed_issues_no_github_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(path, [_group("g_a", "Grupo A", "xa", 1)])

    result = await sc.sync_closed_issues()
    assert "no configurado" in result


async def test_sync_closed_issues_no_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(path, [_group("g_a", "Grupo A", "xa", 1)])
    data = _read(path)
    data["groups"][0]["issue_number"] = 42
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)

    import githubIssues

    monkeypatch.setattr(githubIssues, "list_closed_issues", AsyncMock(return_value=[]))

    result = await sc.sync_closed_issues()
    assert "No hay issues cerrados" in result

    data = _read(path)
    assert len(data["groups"]) == 1


async def test_sync_closed_issues_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "fake-token", raising=False)
    monkeypatch.setattr(config, "GITHUB_REPO", "user/repo", raising=False)

    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(config, "SUGGESTIONS_PATH", str(path), raising=False)
    _seed(path, [_group("g_a", "Grupo A", "xa", 1)])
    data = _read(path)
    data["groups"][0]["issue_number"] = 42
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)

    import githubIssues

    monkeypatch.setattr(
        githubIssues, "list_closed_issues", AsyncMock(return_value=[42])
    )

    result = await sc.sync_closed_issues()
    assert "eliminados" in result

    # Second run: group already gone, no-op
    result = await sc.sync_closed_issues()
    assert "Ningún grupo" in result


# --------------------------------------------------------------------------
# completed field: backward compat only (no longer written by to_dict)
# --------------------------------------------------------------------------
async def test_group_completed_no_longer_written():
    g = sc.Group(
        id="g_test",
        title="Test",
        summary="x",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:00:00Z",
        completed=True,
    )
    d = g.to_dict()
    assert "completed" not in d


async def test_group_completed_default_false():
    g = sc.Group(
        id="g_test",
        title="Test",
        summary="x",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:00:00Z",
    )
    assert g.completed is False


async def test_group_completed_backward_compat():
    d = {
        "id": "g_test",
        "title": "Test",
        "summary": "x",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "submissions": [],
    }
    restored = sc.Group.from_dict(d)
    assert restored.completed is False


async def test_group_completed_backward_compat_reads_old():
    d = {
        "id": "g_test",
        "title": "Test",
        "summary": "x",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "submissions": [],
        "completed": True,
    }
    restored = sc.Group.from_dict(d)
    assert restored.completed is True
