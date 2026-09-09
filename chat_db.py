"""
SQLite database storage for Discord chat history and FTS5 search in VaPls.

Provides full-text search (FTS5) across chat messages, progress tracking
for asynchronous channel history scraping, and live message indexing.
"""

import logging
import sqlite3
import time
from pathlib import Path

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

        CREATE TABLE IF NOT EXISTS scrape_progress (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL DEFAULT 0,
            channel_name TEXT NOT NULL DEFAULT '',
            oldest_message_id INTEGER DEFAULT 0,
            messages_scraped INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            last_scraped_at INTEGER DEFAULT 0
        );
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


def mark_message_deleted(message_id: int) -> bool:
    """Mark a message as deleted in the database."""
    if _conn is None:
        return False
    with _conn:
        cur = _conn.execute("UPDATE messages SET is_deleted=1 WHERE message_id=?", (int(message_id),))
        return cur.rowcount > 0


def mark_messages_deleted_batch(message_ids: list[int]) -> int:
    """Mark multiple messages as deleted in the database."""
    if _conn is None or not message_ids:
        return 0
    count = 0
    with _conn:
        for mid in message_ids:
            cur = _conn.execute("UPDATE messages SET is_deleted=1 WHERE message_id=?", (int(mid),))
            if cur.rowcount > 0:
                count += 1
    return count


def save_messages_batch(messages: list[dict]) -> int:
    """Save a batch of message dicts to database and FTS index.

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
                (
                    msg_id,
                    guild_id,
                    chan_id,
                    chan_name,
                    auth_id,
                    auth_name,
                    content,
                    has_att,
                    created_at,
                ),
            )
            if cur.rowcount > 0:
                inserted_count += 1
                # Insert into FTS
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


def get_scrape_progress(channel_id: int) -> dict | None:
    """Get scraping progress row for a channel."""
    if _conn is None:
        return None
    cur = _conn.execute(
        """SELECT channel_id, guild_id, channel_name, oldest_message_id,
                  messages_scraped, completed, last_scraped_at
           FROM scrape_progress WHERE channel_id=?""",
        (channel_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def update_scrape_progress(
    channel_id: int,
    guild_id: int,
    channel_name: str,
    oldest_message_id: int,
    added_count: int,
    completed: bool = False,
) -> None:
    """Update or insert progress for a channel after a batch scrape."""
    if _conn is None:
        return
    now = int(time.time())
    with _conn:
        _conn.execute(
            """INSERT INTO scrape_progress
               (channel_id, guild_id, channel_name, oldest_message_id,
                messages_scraped, completed, last_scraped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   guild_id = excluded.guild_id,
                   channel_name = excluded.channel_name,
                   oldest_message_id = CASE
                       WHEN excluded.oldest_message_id > 0 THEN excluded.oldest_message_id
                       ELSE scrape_progress.oldest_message_id
                   END,
                   messages_scraped = scrape_progress.messages_scraped + ?,
                   completed = CASE WHEN excluded.completed = 1 THEN 1 ELSE scrape_progress.completed END,
                   last_scraped_at = ?""",
            (
                channel_id,
                guild_id,
                channel_name,
                oldest_message_id,
                added_count,
                1 if completed else 0,
                now,
                added_count,
                now,
            ),
        )


def mark_scrape_completed(channel_id: int) -> None:
    """Mark a channel's historical scraping as 100% completed."""
    if _conn is None:
        return
    with _conn:
        _conn.execute(
            "UPDATE scrape_progress SET completed=1, last_scraped_at=? WHERE channel_id=?",
            (int(time.time()), channel_id),
        )


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
            # Escape or format FTS search terms (simple sanitization)
            sanitized_fts = " ".join(f'"{token.replace(chr(34), "")}"' for token in clean_query.split())
            if sanitized_fts:
                sql = """
                    SELECT m.message_id, m.guild_id, m.channel_id, m.channel_name,
                           m.author_id, m.author_name, m.content, m.has_attachments, m.created_at, m.is_deleted
                    FROM messages_fts f
                    JOIN messages m ON f.message_id = m.message_id
                    WHERE messages_fts MATCH ?
                """
                params = [sanitized_fts]
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
