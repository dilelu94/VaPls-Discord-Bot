"""Persistent archive for Indio Q+A exchanges.

Every successful Indio exchange (slash `/indio` and userbot auto-reply) is
appended to a JSONL queue on disk. A background sweeper reads the queue
periodically and posts entries older than ``INDIO_ARCHIVE_DELAY_SECONDS`` to
the archive thread via the userbot relay — so the archived message keeps
the Indio account identity (and therefore turns up in normal Discord
message search). Failed posts stay in the queue and are retried on the
next sweep; the queue survives restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("indioArchive")


# ---------- Queue persistence (JSONL on disk) ------------------------------

_queue_lock = asyncio.Lock()


def _queue_path() -> Path:
    return Path(config.INDIO_ARCHIVE_QUEUE_PATH)


def _read_queue() -> list[dict]:
    p = _queue_path()
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        logger.exception("indio_archive: read failed")
        return []
    return out


def _write_queue(entries: list[dict]) -> None:
    p = _queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(p.parent), delete=False,
    ) as tmp:
        for e in entries:
            tmp.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp_path = tmp.name
    os.replace(tmp_path, p)


# ---------- Public API: enqueue + format -----------------------------------


async def enqueue(
    *,
    guild_id: Optional[int],
    channel_id: Optional[int],
    speaker: str,
    question: str,
    reply: str,
    ts: Optional[float] = None,
) -> None:
    """Append an Indio exchange to the archive queue.

    Best-effort: any failure is swallowed and logged. Returns immediately
    when archiving is disabled (``INDIO_ARCHIVE_THREAD_ID`` is 0).
    """
    if not getattr(config, "INDIO_ARCHIVE_THREAD_ID", 0):
        return
    entry = {
        "id": uuid.uuid4().hex,
        "ts": float(ts if ts is not None else time.time()),
        "guild_id": int(guild_id) if guild_id else 0,
        "channel_id": int(channel_id) if channel_id else 0,
        "speaker": speaker or "alguien",
        "question": question or "",
        "reply": reply or "",
    }
    async with _queue_lock:
        try:
            entries = _read_queue()
            entries.append(entry)
            _write_queue(entries)
        except Exception:
            logger.exception("indio_archive: enqueue failed")


def format_archive_message(entry: dict) -> str:
    """Reproduce the visual format used by ``indioLogic``: bold speaker
    header, quoted question, blank line, then the reply."""
    speaker = entry.get("speaker") or "alguien"
    question = entry.get("question") or ""
    reply = entry.get("reply") or ""
    lines = question.splitlines() or [""]
    quoted = "\n".join(f"> {ln}" for ln in lines)
    return f"**{speaker}** preguntó:\n{quoted}\n\n{reply}"


# ---------- Posting + sweeping ---------------------------------------------


async def _post_archive(content: str) -> bool:
    """POST one archive line to the userbot's /say relay, targeting the
    archive thread. Returns True on success."""
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    thread_id = int(getattr(config, "INDIO_ARCHIVE_THREAD_ID", 0) or 0)
    if not (url and secret and thread_id):
        return False
    payload = {"channel_id": thread_id, "content": content}
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "indio_archive relay HTTP %d: %s",
                        resp.status, body[:200],
                    )
                    return False
                return True
    except asyncio.TimeoutError:
        logger.warning("indio_archive relay timeout")
        return False
    except Exception:
        logger.exception("indio_archive relay failed")
        return False


async def sweep_once(now: Optional[float] = None) -> int:
    """Post every queued entry older than ``INDIO_ARCHIVE_DELAY_SECONDS``
    and remove the successfully-posted ones from the queue. Returns the
    number of entries archived.

    Entries that fail to post stay in the queue and will be retried next
    sweep. The queue file is re-read at the end so concurrent enqueues
    happening during the HTTP posts don't get clobbered.
    """
    if not getattr(config, "INDIO_ARCHIVE_THREAD_ID", 0):
        return 0
    threshold = float(getattr(config, "INDIO_ARCHIVE_DELAY_SECONDS", 7200))
    if now is None:
        now = time.time()
    cutoff = now - threshold
    async with _queue_lock:
        entries = _read_queue()
    if not entries:
        return 0
    ready = [e for e in entries if float(e.get("ts", 0)) <= cutoff]
    if not ready:
        return 0
    archived_ids: set[str] = set()
    for entry in ready:
        content = format_archive_message(entry)
        if await _post_archive(content):
            archived_ids.add(entry.get("id") or "")
    if not archived_ids:
        return 0
    async with _queue_lock:
        current = _read_queue()
        remaining = [e for e in current if (e.get("id") or "") not in archived_ids]
        _write_queue(remaining)
    return len(archived_ids)


# ---------- Sweeper task lifecycle -----------------------------------------


_sweeper_task: Optional[asyncio.Task] = None


async def _sweep_loop() -> None:
    interval = float(getattr(config, "INDIO_ARCHIVE_SWEEP_INTERVAL_SECONDS", 60))
    interval = max(interval, 1.0)
    while True:
        try:
            n = await sweep_once()
            if n:
                logger.info("indio_archive: archived %d exchange(s)", n)
        except Exception:
            logger.exception("indio_archive sweep loop error")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


def start_sweeper() -> None:
    """Start the background sweeper task. Idempotent. No-op when archiving
    is disabled."""
    global _sweeper_task
    if not getattr(config, "INDIO_ARCHIVE_THREAD_ID", 0):
        return
    if _sweeper_task and not _sweeper_task.done():
        return
    _sweeper_task = asyncio.create_task(_sweep_loop(), name="indio-archive-sweep")


def stop_sweeper() -> None:
    """Cancel the sweeper task if running. Useful for tests."""
    global _sweeper_task
    if _sweeper_task and not _sweeper_task.done():
        _sweeper_task.cancel()
    _sweeper_task = None
