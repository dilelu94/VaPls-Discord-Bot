"""
Activity tracking database with Glicko-based MMR for VaPls userbot.

Tracks all Discord user activities and maintains a converging rating
(Glicko-1 style) per user per guild. Supports spam detection, quality
scoring, and editable weights via the config table.
"""

import json
import math
import sqlite3
import time
from pathlib import Path

DB_PATH = None
_conn: sqlite3.Connection | None = None

DEFAULT_WEIGHTS = {
    "voice_session": 0,
    "voice_vad": 0.4,
    "camera": 0.8,
    "stream": 1.5,
    "watch_stream": 0.1,
    "message": 0.3,
    "image": 0.8,
    "file": 0.6,
    "link": 0.05,
    "tiktok_link": -0.1,
    "sticker": 0.01,
    "thread_post": 1.5,
    "thread_create": 5.0,
    "forum_post": 2.0,
    "forum_create": 8.0,
    "reaction": 0.05,
    "slash_command": 0.05,
    "event_create": 0,
    "event_join": 1.0,
    "channel_create": 0,
    "poll_create": 3.0,
    "poll_vote": 0.15,
}

DEFAULT_CFG = {
    "initial_rating": "1500",
    "initial_deviation": "350",
    "min_deviation": "30",
    "max_deviation": "500",
    "system_rating": "1500",
    "system_deviation": "350",
    "decay_per_day": "10",
    "decay_rating_per_day": "1",
    "spam_window_seconds": "10",
    "spam_max_events": "5",
    "premium_multiplier": "0.85",
    "k_factor": "1.0",
}


def _get_cfg_int(key: str) -> int:
    v = get_config(key)
    try:
        return int(v)
    except (ValueError, TypeError):
        return int(DEFAULT_CFG.get(key, "0"))


def _get_cfg_float(key: str) -> float:
    v = get_config(key)
    try:
        return float(v)
    except (ValueError, TypeError):
        return float(DEFAULT_CFG.get(key, "0.0"))


def init_db(db_path: str | None = None) -> None:
    global DB_PATH, _conn
    DB_PATH = db_path or DB_PATH or "data/activity.db"
    _ensure_dir(DB_PATH)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _schema()
    _purge_old()


def _ensure_dir(path: str) -> None:
    p = Path(path)
    if p.parent != Path("."):
        p.parent.mkdir(parents=True, exist_ok=True)


def _schema() -> None:
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_mmr (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            rating REAL NOT NULL DEFAULT 1500,
            deviation REAL NOT NULL DEFAULT 350,
            volatility REAL NOT NULL DEFAULT 0.06,
            last_activity_at INTEGER NOT NULL DEFAULT 0,
            total_activities INTEGER NOT NULL DEFAULT 0,
            premium INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            display_name TEXT DEFAULT '',
            PRIMARY KEY (user_id, guild_id)
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            activity_type TEXT NOT NULL,
            channel_type TEXT DEFAULT '',
            duration_secs REAL DEFAULT 0,
            quality_score REAL DEFAULT 1.0,
            value REAL DEFAULT 1.0,
            rating_delta REAL DEFAULT 0,
            metadata TEXT DEFAULT '{}',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS raw_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            activity_type TEXT NOT NULL,
            channel_type TEXT DEFAULT '',
            duration_secs REAL DEFAULT 0,
            metadata TEXT DEFAULT '{}',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            voice_seconds REAL DEFAULT 0,
            activity_count INTEGER DEFAULT 0,
            mmr_delta REAL DEFAULT 0,
            peak_rating REAL DEFAULT 1500,
            PRIMARY KEY (user_id, guild_id, date)
        );

        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS activity_log_user_idx
            ON activity_log(user_id, guild_id, created_at);
        CREATE INDEX IF NOT EXISTS activity_log_type_idx
            ON activity_log(activity_type, created_at);
        CREATE INDEX IF NOT EXISTS daily_stats_date_idx
            ON daily_stats(date);
    """)
    _migrate_v1()
    _migrate_v2()
    _migrate_v3()


def _migrate_v1() -> None:
    try:
        _conn.execute("ALTER TABLE user_mmr ADD COLUMN display_name TEXT DEFAULT ''")
    except Exception:
        pass
    for k, v in DEFAULT_WEIGHTS.items():
        cur = _conn.execute("SELECT 1 FROM config WHERE key=?", (f"weight_{k}",))
        if cur.fetchone() is None:
            _conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (f"weight_{k}", str(v)),
            )
    for k, v in DEFAULT_CFG.items():
        _conn.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, str(v))
        )
    _conn.commit()


def _migrate_v2() -> None:
    for k in ("weight_event_create", "weight_channel_create"):
        _conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, '0')", (k,)
        )
    _conn.commit()


def _migrate_v3() -> None:
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS pet_points (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            total_earned REAL NOT NULL DEFAULT 0,
            spent REAL NOT NULL DEFAULT 0,
            reserved REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (user_id, guild_id)
        )
    """)
    _conn.commit()


# ---- Pet evolution points --------------------------------------------------


def earn_pet_points(user_id: int, guild_id: int, amount: float) -> None:
    if _conn is None:
        return
    now = _now()
    _conn.execute(
        """INSERT INTO pet_points (user_id, guild_id, total_earned, spent, reserved, updated_at)
           VALUES (?, ?, ?, 0, 0, ?)
           ON CONFLICT(user_id, guild_id) DO UPDATE SET
               total_earned = total_earned + ?,
               updated_at = ?""",
        (user_id, guild_id, amount, now, amount, now),
    )
    _conn.commit()


def get_pet_points(user_id: int, guild_id: int) -> dict:
    if _conn is None:
        return {"total_earned": 0, "spent": 0, "reserved": 0, "available": 0}
    cur = _conn.execute(
        "SELECT total_earned, spent, reserved FROM pet_points WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    )
    row = cur.fetchone()
    if row is None:
        seed = 200
        now = _now()
        _conn.execute(
            "INSERT INTO pet_points (user_id, guild_id, total_earned, spent, reserved, updated_at) VALUES (?, ?, ?, 0, 0, ?)",
            (user_id, guild_id, seed, now),
        )
        _conn.commit()
        return {"total_earned": seed, "spent": 0, "reserved": 0, "available": seed}
    te = row["total_earned"]
    sp = row["spent"]
    rs = row["reserved"]
    return {"total_earned": te, "spent": sp, "reserved": rs, "available": te - sp - rs}


def reserve_pet_points(user_id: int, guild_id: int, amount: float) -> bool:
    if _conn is None:
        return False
    info = get_pet_points(user_id, guild_id)
    if info["available"] < amount:
        return False
    _conn.execute(
        "UPDATE pet_points SET reserved = reserved + ?, updated_at = ? WHERE user_id=? AND guild_id=?",
        (amount, _now(), user_id, guild_id),
    )
    _conn.commit()
    return True


def release_pet_points(user_id: int, guild_id: int, amount: float) -> bool:
    if _conn is None:
        return False
    _conn.execute(
        "UPDATE pet_points SET reserved = MAX(0, reserved - ?), updated_at = ? WHERE user_id=? AND guild_id=?",
        (amount, _now(), user_id, guild_id),
    )
    _conn.commit()
    return True


def spend_pet_points(user_id: int, guild_id: int, amount: float) -> bool:
    if _conn is None:
        return False
    info = get_pet_points(user_id, guild_id)
    if info["available"] < amount:
        return False
    _conn.execute(
        "UPDATE pet_points SET spent = spent + ?, updated_at = ? WHERE user_id=? AND guild_id=?",
        (amount, _now(), user_id, guild_id),
    )
    _conn.commit()
    return True


def get_config(key: str) -> str:
    if _conn is None:
        return DEFAULT_CFG.get(key, "") or (
            str(DEFAULT_WEIGHTS.get(key.replace("weight_", ""), ""))
            if key.startswith("weight_")
            else ""
        )
    cur = _conn.execute("SELECT value FROM config WHERE key=?", (key,))
    row = cur.fetchone()
    if row:
        return row["value"]
    if key.startswith("weight_"):
        w = key[7:]
        return str(DEFAULT_WEIGHTS.get(w, "0"))
    return DEFAULT_CFG.get(key, "")


def set_config(key: str, value: str) -> None:
    if _conn is None:
        return
    _conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
    )
    _conn.commit()


def get_all_config() -> dict[str, str]:
    if _conn is None:
        return {}
    cur = _conn.execute("SELECT key, value FROM config ORDER BY key")
    return {row["key"]: row["value"] for row in cur.fetchall()}


def _get_weight(activity_type: str) -> float:
    w = get_config(f"weight_{activity_type}")
    try:
        return float(w)
    except (ValueError, TypeError):
        return DEFAULT_WEIGHTS.get(activity_type, 0.0)


def _now() -> int:
    return int(time.time())


# ---- Spam detection -------------------------------------------------------


def _detect_spam(user_id: int, guild_id: int, activity_type: str) -> float:
    """Return a quality multiplier (0.0-1.0) based on recent activity rate.

    Checks how many events of the *same type* occurred in the spam window.
    """
    if _conn is None:
        return 1.0
    window = _get_cfg_int("spam_window_seconds") or 10
    max_events = _get_cfg_int("spam_max_events") or 5
    cutoff = _now() - window
    cur = _conn.execute(
        """SELECT COUNT(*) AS cnt FROM activity_log
           WHERE user_id=? AND guild_id=? AND activity_type=?
           AND created_at > ?""",
        (user_id, guild_id, activity_type, cutoff),
    )
    row = cur.fetchone()
    cnt = row["cnt"] if row else 0
    if cnt >= max_events * 4:
        return 0.1
    if cnt >= max_events * 2:
        return 0.3
    if cnt >= max_events:
        return 0.5
    return 1.0


# ---- MMR math (Glicko-1 style) --------------------------------------------


def _expected_score(
    r: float,
    rd: float,
    opp_r: float = 1500.0,
    opp_rd: float = 350.0,
) -> float:
    total_sq = rd**2 + opp_rd**2
    g = 1.0 / math.sqrt(1.0 + 3.0 * total_sq / (math.pi**2))
    return 1.0 / (1.0 + 10.0 ** (-g * (r - opp_r) / 400.0))


def _glicko_update(
    r: float, rd: float, actual: float, expected: float
) -> tuple[float, float]:
    """Glicko-1 update. Both actual and expected must be 0-1."""
    expected = max(0.01, min(0.99, expected))
    g = 1.0 / math.sqrt(1.0 + 3.0 * rd**2 / (math.pi**2))
    d2 = 1.0 / (g**2 * expected * (1.0 - expected))
    new_r = r + (g / (1.0 / d2 + 1.0 / (rd**2))) * (actual - expected)
    new_rd = math.sqrt(1.0 / (1.0 / d2 + 1.0 / (rd**2)))
    min_rd = _get_cfg_float("min_deviation") or 30.0
    max_rd = _get_cfg_float("max_deviation") or 500.0
    new_rd = max(min_rd, min(max_rd, new_rd))
    return new_r, new_rd


# ---- Activity logging ------------------------------------------------------


def log_activity(
    user_id: int,
    guild_id: int,
    activity_type: str,
    *,
    channel_type: str = "",
    duration_secs: float = 0.0,
    quality_score: float | None = None,
    value: float = 1.0,
    metadata: dict | None = None,
    is_premium: bool = False,
    display_name: str = "",
) -> float:
    """Log an activity and update the user's MMR.

    Returns the rating delta (positive = MMR gain, negative = loss).
    """
    if _conn is None:
        return 0.0
    now = _now()

    spam_q = _detect_spam(user_id, guild_id, activity_type)
    q = quality_score if quality_score is not None else 1.0
    q = q * spam_q
    if is_premium:
        q = q * (_get_cfg_float("premium_multiplier") or 0.85)

    weight = _get_weight(activity_type)
    weight_factor = min(1.0, weight / 4.0)
    actual = 0.5 + (q - 0.5) * weight_factor
    actual = max(0.0, min(1.0, actual))

    # Get current MMR
    cur = _conn.execute(
        "SELECT rating, deviation, total_activities, last_activity_at FROM user_mmr WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    )
    row = cur.fetchone()
    if row:
        r = row["rating"]
        rd = row["deviation"]
        total = row["total_activities"]
        last_at = row["last_activity_at"]
    else:
        r = _get_cfg_float("initial_rating") or 1500.0
        rd = _get_cfg_float("initial_deviation") or 350.0
        total = 0
        last_at = 0

    # Expected: Glicko probability this user "wins" the activity
    expected = _expected_score(r, rd)
    new_r, new_rd = _glicko_update(r, rd, actual, expected)
    delta = new_r - r

    # Apply inactivity decay if more than 1 day since last activity
    if row and now - last_at > 86400:
        days_idle = (now - last_at) / 86400
        decay_rd = _get_cfg_float("decay_per_day") or 10
        decay_r = _get_cfg_float("decay_rating_per_day") or 1
        new_rd = min(
            _get_cfg_float("max_deviation") or 500,
            new_rd + decay_rd * days_idle,
        )
        if new_r > 1500:
            new_r = max(1500, new_r - decay_r * days_idle)
        elif new_r < 1500:
            new_r = min(1500, new_r + decay_r * days_idle)
        delta = new_r - r

    # Persist
    _conn.execute(
        """INSERT OR REPLACE INTO user_mmr
           (user_id, guild_id, rating, deviation, volatility,
            last_activity_at, total_activities, premium, updated_at,
            display_name)
           VALUES (?, ?, ?, ?, 0.06, ?, ?, ?, ?, ?)""",
        (
            user_id,
            guild_id,
            round(new_r, 1),
            round(new_rd, 1),
            now,
            total + 1,
            1 if is_premium else 0,
            now,
            display_name or "",
        ),
    )
    _conn.execute(
        """INSERT INTO activity_log
           (user_id, guild_id, activity_type, channel_type,
            duration_secs, quality_score, value, rating_delta,
            metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            guild_id,
            activity_type,
            channel_type,
            duration_secs,
            round(q, 4),
            value,
            round(delta, 2),
            json.dumps(metadata or {}),
            now,
        ),
    )
    _conn.commit()
    return delta


def _purge_old() -> None:
    cutoff = _now() - 31536000
    for t in ("activity_log", "raw_activity_log"):
        _conn.execute(f"DELETE FROM {t} WHERE created_at < ?", (cutoff,))
    _conn.commit()


# ---- Raw activity logging (unfiltered, before quality mods) -----------------


def log_raw_activity(
    user_id: int,
    guild_id: int,
    activity_type: str,
    *,
    channel_type: str = "",
    duration_secs: float = 0.0,
    metadata: dict | None = None,
) -> None:
    if _conn is None:
        return
    _conn.execute(
        """INSERT INTO raw_activity_log
           (user_id, guild_id, activity_type, channel_type,
            duration_secs, metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            guild_id,
            activity_type,
            channel_type,
            duration_secs,
            json.dumps(metadata or {}),
            _now(),
        ),
    )
    _conn.commit()


# ---- Queries ---------------------------------------------------------------


def get_user_stats(user_id: int, guild_id: int) -> dict | None:
    if _conn is None:
        return None
    cur = _conn.execute(
        """SELECT rating, deviation, volatility, last_activity_at,
                  total_activities, premium, updated_at
           FROM user_mmr WHERE user_id=? AND guild_id=?""",
        (user_id, guild_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    r = dict(row)
    # Recent activity breakdown
    cur2 = _conn.execute(
        """SELECT activity_type, COUNT(*) AS cnt, SUM(rating_delta) AS total_delta
           FROM activity_log
           WHERE user_id=? AND guild_id=?
           AND created_at > ?
           GROUP BY activity_type ORDER BY cnt DESC""",
        (user_id, guild_id, _now() - 86400 * 7),
    )
    r["recent_activities"] = [dict(x) for x in cur2.fetchall()]
    return r


def get_voice_summary(
    user_id: int, guild_id: int, since_ts: int, until_ts: int | None = None
) -> dict | None:
    if _conn is None:
        return None
    if until_ts is None:
        until_ts = _now()
    cur = _conn.execute(
        """SELECT
               COUNT(*) AS sessions,
               COALESCE(SUM(duration_secs), 0) AS total_connected,
               COALESCE(SUM(json_extract(metadata, '$.active_secs')), 0) AS total_active,
               COALESCE(SUM(json_extract(metadata, '$.muted_secs')), 0) AS total_muted,
               COALESCE(SUM(json_extract(metadata, '$.deafened_secs')), 0) AS total_deafened
           FROM activity_log
           WHERE user_id=? AND guild_id=? AND activity_type='voice_session'
           AND created_at >= ? AND created_at <= ?""",
        (user_id, guild_id, since_ts, until_ts),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur2 = _conn.execute(
        """SELECT COALESCE(SUM(duration_secs), 0) AS total_speaking
           FROM activity_log
           WHERE user_id=? AND guild_id=? AND activity_type='voice_vad'
           AND created_at >= ? AND created_at <= ?""",
        (user_id, guild_id, since_ts, until_ts),
    )
    row2 = cur2.fetchone()
    return {
        "sessions": row["sessions"],
        "total_connected": round(row["total_connected"], 1),
        "total_active": round(row["total_active"], 1),
        "total_muted": round(row["total_muted"], 1),
        "total_deafened": round(row["total_deafened"], 1),
        "total_speaking": round(row2["total_speaking"], 1) if row2 else 0,
        "since": since_ts,
        "until": until_ts,
    }


def get_leaderboard(guild_id: int, limit: int = 20) -> list[dict]:
    if _conn is None:
        return []
    cur = _conn.execute(
        """SELECT user_id, rating, deviation, total_activities,
                  last_activity_at, display_name
           FROM user_mmr WHERE guild_id=?
           ORDER BY rating DESC LIMIT ?""",
        (guild_id, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def get_recent_activity(guild_id: int, limit: int = 50) -> list[dict]:
    if _conn is None:
        return []
    cur = _conn.execute(
        """SELECT id, user_id, activity_type, channel_type,
                  duration_secs, quality_score, value, rating_delta,
                  created_at
           FROM activity_log WHERE guild_id=?
           ORDER BY created_at DESC LIMIT ?""",
        (guild_id, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def get_all_data() -> dict:
    """Full DB dump for the admin web UI."""
    if _conn is None:
        return {"mmr": [], "activity": [], "daily": [], "config": {}}
    mmr = [
        dict(row)
        for row in _conn.execute(
            "SELECT * FROM user_mmr ORDER BY rating DESC"
        ).fetchall()
    ]
    activity = [
        dict(row)
        for row in _conn.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
    ]
    raw = [
        dict(row)
        for row in _conn.execute(
            "SELECT * FROM raw_activity_log ORDER BY created_at DESC LIMIT 1000"
        ).fetchall()
    ]
    daily = [
        dict(row)
        for row in _conn.execute(
            "SELECT * FROM daily_stats ORDER BY date DESC LIMIT 200"
        ).fetchall()
    ]
    cfg = get_all_config()
    # Decode metadata fields for activity
    for a in activity:
        try:
            a["metadata"] = json.loads(a["metadata"])
        except (json.JSONDecodeError, TypeError):
            a["metadata"] = {}
    return {"mmr": mmr, "activity": activity, "raw": raw, "daily": daily, "config": cfg}


def get_last_voice_timestamps(guild_id: int) -> dict[int, int]:
    """Return {user_id: last_unix_ts} from voice_session activity, grouped by user."""
    if _conn is None:
        return {}
    cur = _conn.execute(
        """SELECT user_id, MAX(created_at) AS last_seen
           FROM activity_log
           WHERE guild_id=? AND activity_type='voice_session'
           GROUP BY user_id""",
        (guild_id,),
    )
    return {row["user_id"]: row["last_seen"] for row in cur.fetchall()}


def get_premium_users() -> list[int]:
    if _conn is None:
        return []
    cur = _conn.execute("SELECT DISTINCT user_id FROM user_mmr WHERE premium=1")
    return [row["user_id"] for row in cur.fetchall()]


def set_premium(user_id: int, guild_id: int, is_premium: bool) -> None:
    if _conn is None:
        return
    _conn.execute(
        "UPDATE user_mmr SET premium=? WHERE user_id=? AND guild_id=?",
        (1 if is_premium else 0, user_id, guild_id),
    )
    _conn.commit()
