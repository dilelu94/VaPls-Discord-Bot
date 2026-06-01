"""Behavior: when the indio loads its persisted memory from disk, legacy
turns with the old ``[Speaker]:`` bracketed format and Discord emoji codes
get migrated on the fly. No manual JSON edits needed on the server.

Why this matters: production memory was accumulating ``:ahegao:`` shortcodes
that the model imitated, and bracketed ``[Mati]:`` prefixes that taught it
to start replies with ``[Name]:``. After the storage format switch, old
entries still on disk would keep poisoning future turns until evicted by
TTL (6h) — migrating on load fixes that immediately.
"""
from __future__ import annotations

import json
import time


def _write_state(path, history):
    """Build an indio_memory.json with one guild bucket carrying ``history``."""
    payload = {
        "entries": {
            "guild-100": {
                "history": history,
                "last_seen": time.time(),
                "long_term": {},
                "current_members": [],
                "current_members_refreshed_at": 0.0,
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_bracketed_speaker_rewritten_to_new_format(indio, tmp_path):
    """``[Mati]: hola`` on disk → ``Mati: hola`` in memory, so the model
    still knows who said what."""
    state_path = tmp_path / "indio_memory.json"
    import config
    config.INDIO_MEMORY_PATH = str(state_path)

    _write_state(state_path, [
        {"role": "user", "parts": [{"text": "[Mati]: hola"}], "ts": time.time()},
    ])

    indio._indio_history.clear()
    indio._load_indio_state()

    loaded_text = indio._indio_history["guild-100"][0]["parts"][0]["text"]
    assert "[Mati]:" not in loaded_text
    assert loaded_text.startswith("Mati:")
    assert "hola" in loaded_text


def test_legacy_emoji_markup_stripped_on_load(indio, tmp_path):
    """``<:ahegao:765>`` on disk gets stripped during load."""
    state_path = tmp_path / "indio_memory.json"
    import config
    config.INDIO_MEMORY_PATH = str(state_path)

    _write_state(state_path, [
        {"role": "model", "parts": [{"text": "jaja <:ahegao:765> posta"}], "ts": time.time()},
    ])

    indio._indio_history.clear()
    indio._load_indio_state()

    loaded_text = indio._indio_history["guild-100"][0]["parts"][0]["text"]
    assert "<:ahegao:765>" not in loaded_text
    assert "jaja" in loaded_text
    assert "posta" in loaded_text


def test_legacy_shortcode_stripped_on_load(indio, tmp_path):
    """Bare shortcodes like ``:ahegao:`` get cleaned on load too."""
    state_path = tmp_path / "indio_memory.json"
    import config
    config.INDIO_MEMORY_PATH = str(state_path)

    _write_state(state_path, [
        {"role": "model", "parts": [{"text": "jaja :ahegao: posta"}], "ts": time.time()},
    ])

    indio._indio_history.clear()
    indio._load_indio_state()

    loaded_text = indio._indio_history["guild-100"][0]["parts"][0]["text"]
    assert ":ahegao:" not in loaded_text
    assert "jaja" in loaded_text


def test_timestamp_survives_legacy_migration(indio, tmp_path):
    """Migration must not strip the ``ts`` field — the temporal-tag feature
    depends on it for ``[hace X]`` rendering."""
    state_path = tmp_path / "indio_memory.json"
    import config
    config.INDIO_MEMORY_PATH = str(state_path)

    ts = time.time() - 60
    _write_state(state_path, [
        {"role": "user", "parts": [{"text": "[Mati]: vieja charla"}], "ts": ts},
    ])

    indio._indio_history.clear()
    indio._load_indio_state()

    loaded_turn = indio._indio_history["guild-100"][0]
    assert loaded_turn["ts"] == ts
    assert loaded_turn["role"] == "user"
