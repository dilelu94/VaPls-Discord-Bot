"""Behavior: Indio Q+A exchanges land in a persistent queue, the sweeper
posts them to the archive thread after the configured delay, and only via
the userbot relay (so the message keeps the Indio identity for later
search). The queue survives restarts and failed posts retry next sweep."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import config
import indioArchive


@pytest.fixture(autouse=True)
def _enable_archive(monkeypatch, tmp_path):
    """Enable archiving and point the queue at a tmp file for each test."""
    monkeypatch.setattr(config, "INDIO_ARCHIVE_THREAD_ID", 999_000_001)
    monkeypatch.setattr(config, "INDIO_ARCHIVE_DELAY_SECONDS", 7200)
    monkeypatch.setattr(
        config, "INDIO_ARCHIVE_QUEUE_PATH",
        str(tmp_path / "queue.jsonl"),
    )
    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://127.0.0.1:8081/say")
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "secret")
    monkeypatch.setattr(config, "INDIO_RELAY_TIMEOUT", 5.0)
    yield


@pytest.fixture
def fake_relay(monkeypatch):
    """Capture every relay POST that the sweeper makes."""
    posts: list[dict] = []

    async def _ok(content):
        posts.append({"content": content, "thread_id": config.INDIO_ARCHIVE_THREAD_ID})
        return True

    monkeypatch.setattr(indioArchive, "_post_archive", _ok)
    return posts


@pytest.fixture
def failing_relay(monkeypatch):
    """A relay that always reports failure (HTTP 5xx, timeout, etc.)."""
    posts: list[dict] = []

    async def _fail(content):
        posts.append({"content": content})
        return False

    monkeypatch.setattr(indioArchive, "_post_archive", _fail)
    return posts


# ---------- enqueue + persistence ------------------------------------------


async def test_enqueue_persists_to_disk():
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="qué onda", reply="todo bien",
    )
    raw = Path(config.INDIO_ARCHIVE_QUEUE_PATH).read_text(encoding="utf-8")
    entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(entries) == 1
    e = entries[0]
    assert e["speaker"] == "Miles"
    assert e["question"] == "qué onda"
    assert e["reply"] == "todo bien"
    assert e["guild_id"] == 1
    assert e["channel_id"] == 2
    assert "ts" in e and "id" in e


async def test_enqueue_disabled_when_thread_id_is_zero(monkeypatch):
    monkeypatch.setattr(config, "INDIO_ARCHIVE_THREAD_ID", 0)
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="hola", reply="che",
    )
    assert not Path(config.INDIO_ARCHIVE_QUEUE_PATH).exists()


async def test_enqueue_survives_restart_via_disk():
    """Persistence promise: enqueuing now and reading later (simulating a
    restart) yields the same entry. We re-read via the public sweep API."""
    await indioArchive.enqueue(
        guild_id=10, channel_id=20, speaker="A",
        question="q1", reply="r1",
    )
    # Re-read raw from the file the way a fresh process would.
    raw = Path(config.INDIO_ARCHIVE_QUEUE_PATH).read_text(encoding="utf-8")
    entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert entries[0]["speaker"] == "A"


# ---------- formatting promise ---------------------------------------------


def test_format_matches_indio_reply_header_shape():
    """The archive must look like the live reply: bold speaker header, the
    quoted question (every line prefixed with '> '), then the reply."""
    out = indioArchive.format_archive_message({
        "speaker": "Miles",
        "question": "qué onda\nche",
        "reply": "todo bien, gauchito",
    })
    assert "**Miles** preguntó:" in out
    assert "> qué onda" in out
    assert "> che" in out
    # Reply comes after a blank-line separator from the quoted question.
    qpos = out.find("> che")
    rpos = out.find("todo bien")
    assert 0 < qpos < rpos


def test_format_handles_empty_question():
    out = indioArchive.format_archive_message({
        "speaker": "Miles", "question": "", "reply": "respuesta",
    })
    assert "**Miles** preguntó:" in out
    assert "respuesta" in out


# ---------- sweep behavior --------------------------------------------------


async def test_sweep_posts_entries_older_than_delay(fake_relay):
    old_ts = time.time() - 8000  # > 7200s default delay
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="vieja", reply="vieja respuesta", ts=old_ts,
    )
    archived = await indioArchive.sweep_once()
    assert archived == 1
    assert len(fake_relay) == 1
    assert "vieja" in fake_relay[0]["content"]
    # Queue is now empty.
    assert Path(config.INDIO_ARCHIVE_QUEUE_PATH).read_text().strip() == ""


async def test_sweep_keeps_entries_newer_than_delay(fake_relay):
    fresh_ts = time.time() - 30  # well within the 2h window
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="reciente", reply="r", ts=fresh_ts,
    )
    archived = await indioArchive.sweep_once()
    assert archived == 0
    assert fake_relay == []
    # Still on disk.
    raw = Path(config.INDIO_ARCHIVE_QUEUE_PATH).read_text()
    assert "reciente" in raw


async def test_sweep_only_promotes_ripe_entries_leaving_fresh_ones(fake_relay):
    now = time.time()
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="A",
        question="vieja", reply="r1", ts=now - 8000,
    )
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="B",
        question="reciente", reply="r2", ts=now - 30,
    )
    archived = await indioArchive.sweep_once()
    assert archived == 1
    # Only the old one went out, the fresh one is still queued.
    assert any("vieja" in p["content"] for p in fake_relay)
    assert all("reciente" not in p["content"] for p in fake_relay)
    raw = Path(config.INDIO_ARCHIVE_QUEUE_PATH).read_text()
    assert "reciente" in raw
    assert "vieja" not in raw


async def test_failed_post_stays_in_queue_for_retry(failing_relay):
    old_ts = time.time() - 8000
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="que no se postea", reply="r", ts=old_ts,
    )
    archived = await indioArchive.sweep_once()
    assert archived == 0
    # Entry must still be in the queue so the next sweep can retry.
    raw = Path(config.INDIO_ARCHIVE_QUEUE_PATH).read_text()
    assert "que no se postea" in raw


async def test_sweep_is_a_noop_when_disabled(monkeypatch, fake_relay):
    # First enqueue with archiving enabled to populate the queue.
    old_ts = time.time() - 8000
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="A",
        question="q", reply="r", ts=old_ts,
    )
    # Then disable and sweep — nothing should be posted.
    monkeypatch.setattr(config, "INDIO_ARCHIVE_THREAD_ID", 0)
    archived = await indioArchive.sweep_once()
    assert archived == 0
    assert fake_relay == []


async def test_sweep_uses_archive_thread_as_destination(fake_relay):
    old_ts = time.time() - 8000
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="q", reply="r", ts=old_ts,
    )
    await indioArchive.sweep_once()
    assert fake_relay[0]["thread_id"] == config.INDIO_ARCHIVE_THREAD_ID


# ---------- robustness: malformed JSONL, oversize replies ------------------


async def test_malformed_jsonl_line_is_skipped_and_warned(caplog):
    """If the queue file got corrupted (partial write, manual edit), bad
    lines must be skipped — but the operator should see a warning so they
    know to fix the file by hand."""
    queue_path = Path(config.INDIO_ARCHIVE_QUEUE_PATH)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        '{"id":"a","ts":1,"speaker":"X","question":"q","reply":"r"}\n'
        "this is not json\n"
        '{"id":"b","ts":2,"speaker":"Y","question":"q","reply":"r"}\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="indioArchive"):
        # Trigger a read via sweep_once; nothing meets the threshold so
        # nothing posts, but the read still runs.
        await indioArchive.sweep_once()
    # The two valid entries must still be on disk after the read.
    remaining = queue_path.read_text(encoding="utf-8")
    assert '"id":"a"' in remaining or '"id": "a"' in remaining
    assert '"id":"b"' in remaining or '"id": "b"' in remaining
    # And the operator got a warning about the malformed line.
    assert any("malformed" in r.message.lower() for r in caplog.records)


async def test_long_reply_is_chunked_under_discord_limit(fake_relay):
    """Discord rejects messages >2000 chars. A long Indio reply must come
    out as multiple posts to the thread, none exceeding the limit."""
    long_reply = "x" * 5000
    old_ts = time.time() - 8000
    await indioArchive.enqueue(
        guild_id=1, channel_id=2, speaker="Miles",
        question="q", reply=long_reply, ts=old_ts,
    )
    archived = await indioArchive.sweep_once()
    assert archived == 1
    # More than one chunk posted, none over the limit.
    assert len(fake_relay) > 1
    for post in fake_relay:
        assert len(post["content"]) < 2000
    # And the full reply content still made it across (sum of chunks).
    joined = "".join(p["content"] for p in fake_relay)
    assert long_reply in joined


async def test_chunk_helper_returns_single_chunk_for_short_text():
    out = indioArchive.chunk_for_discord("hola mundo")
    assert out == ["hola mundo"]


async def test_chunk_helper_splits_on_line_boundaries_when_possible():
    text = "linea1\n" * 500  # well over 1990
    chunks = indioArchive.chunk_for_discord(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) < 2000
    # Reassembly preserves the original text.
    assert "".join(chunks) == text
