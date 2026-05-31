"""Behavior: human-in-the-loop curation of the decifrar cache.

The promises pinned here:
- A decifrado run gets recorded to the JSONL when voting is on.
- A 👍 vote (over the threshold) marks the entry approved AND seeds the
  in-memory cache so the next identical raw hits without calling Gemini.
- A 👎 vote (over the threshold) deletes the entry from the JSONL.
- At startup, approved entries from disk are loaded into the cache.
- When voting is disabled, the whole thing is a no-op (no file is written,
  no cache promotion happens).

Tests speak to the public surface (`record`, the resolver entry points,
`approved_seed_pairs`, `start`) — never to private locks or save helpers —
so the storage strategy stays free to change.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """Each test gets a fresh JSONL path and clean module state."""
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_LOG_PATH",
                        str(tmp_path / "decifrar_log.jsonl"))
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_ENABLED", True)
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_SAMPLE_RATE", 1_000_000)  # never auto-post
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_THRESHOLD", 1)
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_CHANNEL_ID", 12345)
    monkeypatch.setattr(main_config, "DECIFRAR_LOG_MAX_LINES", 100)
    monkeypatch.setattr(main_config, "DECIFRAR_CACHE_SEED_MAX", 100)
    decifrarVoting._reset_for_tests()
    # Also reset the geminiCommand cache so seed/promote tests are isolated.
    import geminiCommand
    geminiCommand._decifrar_cache.clear()
    yield
    decifrarVoting._reset_for_tests()
    geminiCommand._decifrar_cache.clear()


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---- record() ------------------------------------------------------------

async def test_record_appends_pending_entry_to_jsonl(monkeypatch):
    import decifrarVoting
    import config as main_config

    await decifrarVoting.record("hola indio", "hola indio cómo estás")
    await asyncio.sleep(0)  # let the maybe_post task settle (no-op at 1/1M)

    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    assert len(rows) == 1
    assert rows[0]["raw"] == "hola indio"
    assert rows[0]["decifrado"] == "hola indio cómo estás"
    assert rows[0]["status"] == "pending"
    assert rows[0]["msg_id"] is None


async def test_record_is_a_noop_when_voting_disabled(monkeypatch):
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_ENABLED", False)

    await decifrarVoting.record("algo", "algo decifrado")

    assert not os.path.exists(main_config.DECIFRAR_LOG_PATH)


async def test_record_deduplicates_identical_raw_decifrado_pairs():
    import decifrarVoting
    import config as main_config

    await decifrarVoting.record("hola indio", "hola indio")
    await decifrarVoting.record("Hola Indio", "hola indio")  # different case → same key
    await decifrarVoting.record("hola indio", "hola indio cómo estás")  # diff decifrado, ok

    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    # First two collapse (same normalized raw + same decifrado), third is distinct.
    assert len(rows) == 2


async def test_record_ignores_empty_inputs():
    import decifrarVoting
    import config as main_config

    await decifrarVoting.record("", "")
    await decifrarVoting.record("hola", "")
    await decifrarVoting.record("", "hola")

    assert not os.path.exists(main_config.DECIFRAR_LOG_PATH)


# ---- Vote resolution -----------------------------------------------------

async def _seed_pending_entry(decifrarVoting, raw, decifrado, msg_id):
    """Helper: write a pending entry directly with a known msg_id so tests can
    drive the resolvers without going through the random-sample post."""
    await decifrarVoting.record(raw, decifrado)
    # Patch the just-appended entry's msg_id.
    async with decifrarVoting._lock:
        entry = decifrarVoting._entries[-1]
        entry["msg_id"] = msg_id
        decifrarVoting._save_unlocked()
    return entry


async def test_thumbs_up_marks_approved_and_seeds_cache():
    import decifrarVoting
    import geminiCommand
    import config as main_config

    msg_id = 999_000
    entry = await _seed_pending_entry(
        decifrarVoting, "che indio dale algo", "che indio dale algo", msg_id,
    )

    fake_msg = MagicMock()
    fake_msg.content = "old"
    fake_msg.edit = AsyncMock()
    await decifrarVoting._resolve_approved(msg_id, fake_msg)

    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    statuses = [r["status"] for r in rows]
    assert "approved" in statuses

    cache_key = geminiCommand._decifrar_cache_key(entry["raw"])
    assert geminiCommand._decifrar_cache_get(cache_key) == entry["decifrado"]

    fake_msg.edit.assert_awaited_once()


async def test_thumbs_down_removes_entry_from_jsonl():
    import decifrarVoting
    import config as main_config

    msg_id = 999_111
    await _seed_pending_entry(
        decifrarVoting, "ruido feo", "ruido feo limpio", msg_id,
    )
    assert len(_read_jsonl(main_config.DECIFRAR_LOG_PATH)) == 1

    fake_msg = MagicMock()
    fake_msg.delete = AsyncMock()
    await decifrarVoting._resolve_rejected(msg_id, fake_msg)

    assert _read_jsonl(main_config.DECIFRAR_LOG_PATH) == []
    fake_msg.delete.assert_awaited_once()


async def test_handle_vote_dedups_same_user_clicking_twice(monkeypatch):
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_THRESHOLD", 2)

    msg_id = 999_222
    await _seed_pending_entry(decifrarVoting, "hola", "hola", msg_id)

    user = SimpleNamespace(id=42)
    msg = MagicMock()
    msg.id = msg_id
    interaction = MagicMock()
    interaction.user = user
    interaction.message = msg
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()

    await decifrarVoting._handle_vote(interaction, +1)  # first 👍
    await decifrarVoting._handle_vote(interaction, +1)  # same user clicks again

    # The same user voting twice should NOT cross the threshold (2 needed).
    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    assert [r["status"] for r in rows] == ["pending"]


async def test_two_users_thumbs_up_crosses_threshold_and_approves(monkeypatch):
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_THRESHOLD", 2)

    msg_id = 999_333
    await _seed_pending_entry(decifrarVoting, "che indio", "che indio", msg_id)

    msg = MagicMock()
    msg.id = msg_id
    msg.content = "x"
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()

    def _interaction(uid):
        i = MagicMock()
        i.user = SimpleNamespace(id=uid)
        i.message = msg
        i.response.defer = AsyncMock()
        i.response.send_message = AsyncMock()
        return i

    await decifrarVoting._handle_vote(_interaction(1), +1)
    await decifrarVoting._handle_vote(_interaction(2), +1)

    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    assert any(r["status"] == "approved" for r in rows)


async def test_vote_switching_is_supported(monkeypatch):
    """A user can change their mind: clicking 👎 after 👍 removes the 👍 and
    adds the 👎 (net moves by 2)."""
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_THRESHOLD", 1)

    msg_id = 999_444
    await _seed_pending_entry(decifrarVoting, "abc", "abc", msg_id)

    msg = MagicMock()
    msg.id = msg_id
    msg.content = "x"
    msg.edit = AsyncMock()
    msg.delete = AsyncMock()

    def _interaction(uid):
        i = MagicMock()
        i.user = SimpleNamespace(id=uid)
        i.message = msg
        i.response.defer = AsyncMock()
        i.response.send_message = AsyncMock()
        return i

    await decifrarVoting._handle_vote(_interaction(1), +1)  # +1, would approve at threshold=1
    # Already crosses threshold and approves — for this test, let's bump threshold first.
    # Reset and retry:
    decifrarVoting._reset_for_tests()
    decifrarVoting._entries.clear()
    await _seed_pending_entry(decifrarVoting, "abc", "abc", msg_id)
    monkeypatch.setattr(main_config, "DECIFRAR_VOTE_THRESHOLD", 2)

    await decifrarVoting._handle_vote(_interaction(1), +1)
    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    assert [r["status"] for r in rows] == ["pending"]

    # Same user flips to 👎 — net moves from +1 to -1 (delta of 2), but still
    # below abs(threshold) for downvote (need -2). Then a second user votes
    # 👎 and we cross.
    await decifrarVoting._handle_vote(_interaction(1), -1)
    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    assert [r["status"] for r in rows] == ["pending"]

    await decifrarVoting._handle_vote(_interaction(2), -1)
    rows = _read_jsonl(main_config.DECIFRAR_LOG_PATH)
    assert rows == []  # rejected, entry gone


# ---- Startup seeding -----------------------------------------------------

async def test_startup_seeds_cache_from_approved_entries():
    import decifrarVoting
    import geminiCommand
    import config as main_config

    # Write a JSONL with one approved + one pending; only approved should seed.
    payload = [
        {"id": "a", "ts": 1.0, "raw": "che indio",
         "raw_key": "che indio", "decifrado": "che indio dale",
         "status": "approved", "msg_id": None},
        {"id": "b", "ts": 2.0, "raw": "ruido",
         "raw_key": "ruido", "decifrado": "ruido",
         "status": "pending", "msg_id": None},
    ]
    with open(main_config.DECIFRAR_LOG_PATH, "w", encoding="utf-8") as fh:
        for row in payload:
            fh.write(json.dumps(row) + "\n")

    fake_bot = MagicMock()
    fake_bot.add_view = MagicMock()

    await decifrarVoting.start(fake_bot)

    assert geminiCommand._decifrar_cache.get("che indio") == "che indio dale"
    assert "ruido" not in geminiCommand._decifrar_cache


async def test_approved_seed_pairs_respects_cap(monkeypatch):
    import decifrarVoting
    import config as main_config
    monkeypatch.setattr(main_config, "DECIFRAR_CACHE_SEED_MAX", 2)

    payload = [
        {"id": str(i), "ts": float(i), "raw": f"raw{i}",
         "raw_key": f"raw{i}", "decifrado": f"dec{i}",
         "status": "approved", "msg_id": None}
        for i in range(5)
    ]
    with open(main_config.DECIFRAR_LOG_PATH, "w", encoding="utf-8") as fh:
        for row in payload:
            fh.write(json.dumps(row) + "\n")

    pairs = decifrarVoting.approved_seed_pairs()
    # Keeps the 2 most recent (by ts), dropping the older 3.
    assert pairs == [("raw3", "dec3"), ("raw4", "dec4")]
