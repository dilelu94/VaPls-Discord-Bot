"""
SQLite database storage for Discord chat history and FTS5 search in VaPls.

Provides full-text search (FTS5) across chat messages and live message
indexing. Historical backfilling was completed on 2026-09-10 (64 551 messages
across 11 channels). From that point on, only new incoming messages are stored.
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)

DB_PATH: str | None = None
_conn: sqlite3.Connection | None = None


def _ensure_dir(path: str) -> None:
    p = Path(path)
    if p.parent != Path("."):
        p.parent.mkdir(parents=True, exist_ok=True)


def init_db(db_path: str | None = None) -> None:
    """Initialize the chat history SQLite database and schemas."""
    global DB_PATH, _conn
    DB_PATH = db_path or DB_PATH or "data/chat_history.db"
    _ensure_dir(DB_PATH)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _schema()


def close_db() -> None:
    """Close the database connection if open."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


def _schema() -> None:
    if _conn is None:
        return
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL DEFAULT '',
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            has_attachments INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            is_deleted INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_messages_channel_created
            ON messages(channel_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_author
            ON messages(author_id);
        CREATE INDEX IF NOT EXISTS idx_messages_guild
            ON messages(guild_id);
    """)

    # Migration for existing databases without is_deleted column
    try:
        _conn.execute("ALTER TABLE messages ADD COLUMN is_deleted INTEGER DEFAULT 0")
    except Exception:
        pass

    # Create FTS5 table if supported by sqlite runtime
    try:
        _conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                message_id UNINDEXED,
                content,
                author_name,
                channel_name
            );
        """)
    except Exception as e:
        logger.warning("FTS5 table creation skipped or unsupported: %s", e)

    _conn.commit()


# ---------------------------------------------------------------------------
# Message filtering and formatting
# ---------------------------------------------------------------------------

def should_index_message(msg: "discord.Message") -> bool:
    """Return True if a Discord message should be indexed into the DB.

    Excludes bots, the userbot (Indio), the GoLive account, and messages
    that contain neither text nor attachments.
    """
    if msg is None or not getattr(msg, "author", None):
        return False

    author = msg.author
    if getattr(author, "bot", False):
        return False
    if author.id in (config.USERBOT_USER_ID, config.GOLIVE_USER_ID):
        return False

    content = (msg.content or "").strip()
    has_attachments = bool(getattr(msg, "attachments", None))
    if not content and not has_attachments:
        return False

    return True


def format_message_dict(msg: "discord.Message") -> dict:
    """Convert a discord.Message into a dict suitable for save_message()."""
    author = getattr(msg, "author", None)
    author_name = (
        getattr(author, "display_name", None)
        or getattr(author, "name", "alguien")
        if author
        else "alguien"
    )
    guild_id = getattr(getattr(msg, "guild", None), "id", 0)
    channel_name = getattr(getattr(msg, "channel", None), "name", "")
    created_at = (
        int(msg.created_at.timestamp())
        if hasattr(msg, "created_at") and msg.created_at
        else int(time.time())
    )

    return {
        "message_id": msg.id,
        "guild_id": guild_id,
        "channel_id": msg.channel.id,
        "channel_name": channel_name,
        "author_id": author.id if author else 0,
        "author_name": author_name,
        "content": msg.content or "",
        "has_attachments": 1 if getattr(msg, "attachments", None) else 0,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def mark_message_deleted(message_id: int) -> bool:
    """Mark a message as deleted in the database."""
    if _conn is None:
        return False
    with _conn:
        cur = _conn.execute(
            "UPDATE messages SET is_deleted=1 WHERE message_id=?", (int(message_id),)
        )
        return cur.rowcount > 0


def mark_messages_deleted_batch(message_ids: list[int]) -> int:
    """Mark multiple messages as deleted in the database."""
    if _conn is None or not message_ids:
        return 0
    count = 0
    with _conn:
        for mid in message_ids:
            cur = _conn.execute(
                "UPDATE messages SET is_deleted=1 WHERE message_id=?", (int(mid),)
            )
            if cur.rowcount > 0:
                count += 1
    return count


def save_messages_batch(messages: list[dict]) -> int:
    """Save a batch of message dicts to the database and FTS index.

    Each dict must have:
    - message_id (int)
    - guild_id (int)
    - channel_id (int)
    - channel_name (str)
    - author_id (int)
    - author_name (str)
    - content (str)
    - created_at (int)
    - has_attachments (int, optional)

    Returns the number of new messages stored.
    """
    if _conn is None or not messages:
        return 0

    inserted_count = 0
    with _conn:
        for m in messages:
            msg_id = int(m["message_id"])
            guild_id = int(m.get("guild_id") or 0)
            chan_id = int(m["channel_id"])
            chan_name = str(m.get("channel_name") or "")
            auth_id = int(m["author_id"])
            auth_name = str(m.get("author_name") or "")
            content = str(m.get("content") or "").strip()
            has_att = 1 if m.get("has_attachments") else 0
            created_at = int(m.get("created_at") or int(time.time()))

            if not content and not has_att:
                continue

            cur = _conn.execute(
                """INSERT OR IGNORE INTO messages
                   (message_id, guild_id, channel_id, channel_name,
                    author_id, author_name, content, has_attachments, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, guild_id, chan_id, chan_name, auth_id, auth_name,
                 content, has_att, created_at),
            )
            if cur.rowcount > 0:
                inserted_count += 1
                try:
                    _conn.execute(
                        """INSERT OR REPLACE INTO messages_fts
                           (message_id, content, author_name, channel_name)
                           VALUES (?, ?, ?, ?)""",
                        (msg_id, content, auth_name, chan_name),
                    )
                except Exception:
                    pass

    return inserted_count


def save_message(msg_data: dict) -> bool:
    """Save a single message dict."""
    return save_messages_batch([msg_data]) > 0


def total_messages_indexed() -> int:
    """Return the total number of messages currently stored."""
    if _conn is None:
        return 0
    cur = _conn.execute("SELECT COUNT(*) FROM messages")
    row = cur.fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_messages(
    query: str,
    author_name: str | None = None,
    channel_name: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search messages using FTS5 (or LIKE fallback) with optional filters.

    Args:
        query: Full-text search term or keywords.
        author_name: Substring filter on author's display name.
        channel_name: Substring filter on channel name (e.g. 'soreteposting').
        limit: Max results (default 20).

    Returns:
        List of dicts representing matched messages sorted by recency.
    """
    if _conn is None:
        return []

    limit = max(1, min(limit, 100))
    clean_query = (query or "").strip()
    author_filter = (author_name or "").strip()
    chan_filter = (channel_name or "").strip().lstrip("#")

    results = []

    # Attempt FTS search first if query is provided
    fts_working = False
    if clean_query:
        try:
            sanitized_fts = " ".join(
                f'"{token.replace(chr(34), "")}"' for token in clean_query.split()
            )
            if sanitized_fts:
                sql = """
                    SELECT m.message_id, m.guild_id, m.channel_id, m.channel_name,
                           m.author_id, m.author_name, m.content, m.has_attachments,
                           m.created_at, m.is_deleted
                    FROM messages_fts f
                    JOIN messages m ON f.message_id = m.message_id
                    WHERE messages_fts MATCH ?
                """
                params: list = [sanitized_fts]
                if author_filter:
                    sql += " AND m.author_name LIKE ?"
                    params.append(f"%{author_filter}%")
                if chan_filter:
                    sql += " AND m.channel_name LIKE ?"
                    params.append(f"%{chan_filter}%")
                sql += " ORDER BY m.created_at DESC LIMIT ?"
                params.append(limit)

                cur = _conn.execute(sql, params)
                results = [dict(row) for row in cur.fetchall()]
                fts_working = True
        except Exception as e:
            logger.debug("FTS match failed, falling back to standard SQL: %s", e)

    # Fallback to standard SQL query if FTS was not used or failed
    if not fts_working:
        sql = """
            SELECT message_id, guild_id, channel_id, channel_name,
                   author_id, author_name, content, has_attachments, created_at, is_deleted
            FROM messages
            WHERE 1=1
        """
        params = []
        if clean_query:
            sql += " AND content LIKE ?"
            params.append(f"%{clean_query}%")
        if author_filter:
            sql += " AND author_name LIKE ?"
            params.append(f"%{author_filter}%")
        if chan_filter:
            sql += " AND channel_name LIKE ?"
            params.append(f"%{chan_filter}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cur = _conn.execute(sql, params)
        results = [dict(row) for row in cur.fetchall()]

    return results
