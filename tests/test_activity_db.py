"""Tests for the activity tracking and MMR system."""

import json
import math
import sqlite3
import time

import pytest

from userbot import activity_db as adb


# ---- helpers ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Replace the global DB with a fresh in-memory database before each test."""
    monkeypatch.setattr(adb, "_conn", None)
    monkeypatch.setattr(adb, "DB_PATH", None)
    db_path = str(tmp_path / "test_activity.db")
    adb.init_db(db_path)
    yield
    if adb._conn:
        adb._conn.close()
    monkeypatch.setattr(adb, "_conn", None)


def _count_rows(table: str) -> int:
    cur = adb._conn.execute(f"SELECT COUNT(*) AS c FROM {table}")
    return cur.fetchone()["c"]


def _get_mmr(user_id: int = 1, guild_id: int = 100) -> dict | None:
    return adb.get_user_stats(user_id, guild_id)


# ---- schema tests ----------------------------------------------------------


class TestSchema:
    def test_tables_exist(self):
        cur = adb._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cur.fetchall()}
        assert "user_mmr" in tables
        assert "activity_log" in tables
        assert "daily_stats" in tables
        assert "config" in tables

    def test_config_seeded(self):
        cfg = adb.get_all_config()
        assert cfg["initial_rating"] == "1500"
        assert cfg["weight_voice_vad"] == "0.4"


# ---- MMR math tests -------------------------------------------------------


class TestMMRMath:
    def test_expected_score_center(self):
        """Two 1500-rated players have 50% expected score."""
        e = adb._expected_score(1500, 350)
        assert e == pytest.approx(0.5, abs=0.01)

    def test_expected_score_higher(self):
        """A 1700-rated player has >50% expected score."""
        e = adb._expected_score(1700, 350)
        assert e > 0.5

    def test_expected_score_lower(self):
        """A 1300-rated player has <50% expected score."""
        e = adb._expected_score(1300, 350)
        assert e < 0.5

    def test_glicko_update_no_change_when_expected_met(self):
        """If actual==expected, rating changes little."""
        r, rd = adb._glicko_update(1500, 350, 0.5, 0.5)
        assert r == pytest.approx(1500, abs=5)
        assert rd < 350  # deviation shrinks

    def test_glicko_update_upset(self):
        """When actual exceeds expected, rating goes up."""
        r, rd = adb._glicko_update(1500, 350, 0.9, 0.5)
        assert r > 1500
        assert rd < 350

    def test_glicko_update_bad(self):
        """When actual falls short, rating goes down."""
        r, rd = adb._glicko_update(1500, 350, 0.1, 0.5)
        assert r < 1500

    def test_deviation_bounded(self):
        """Deviation stays within [min_deviation, max_deviation]."""
        _, rd = adb._glicko_update(1500, 10, 0.9, 0.5)
        assert rd >= 30  # min_deviation

    def test_multiple_updates_converge(self):
        """After many positive updates, rating stabilizes (does not explode)."""
        r, rd = 1500.0, 350.0
        for _ in range(50):
            r, rd = adb._glicko_update(r, rd, 0.7, 0.5)
        assert 1500 < r < 2300  # converges, doesn't go infinite
        assert rd < 150  # high confidence


# ---- spam detection tests -------------------------------------------------


class TestSpamDetection:
    def test_no_spam_single(self):
        """Single event is not spam."""
        q = adb._detect_spam(1, 100, "message")
        assert q == pytest.approx(1.0)

    def test_spam_after_many(self):
        """Many events in quick succession get penalized."""
        for _ in range(6):
            adb.log_activity(1, 100, "message", value=0.5)
        # Next one should be penalized
        q = adb._detect_spam(1, 100, "message")
        assert q < 1.0

    def test_spam_per_type_independent(self):
        """Spam detection is per activity type."""
        for _ in range(6):
            adb.log_activity(1, 100, "voice_vad", duration_secs=5)
        # Voice is spammed but message shouldn't be
        q = adb._detect_spam(1, 100, "message")
        assert q == pytest.approx(1.0)

    def test_spam_diff_users(self):
        """Spam detection is per user."""
        for _ in range(6):
            adb.log_activity(1, 100, "reaction")
        # Different user shouldn't be penalized
        q = adb._detect_spam(2, 100, "reaction")
        assert q == pytest.approx(1.0)

    def test_spam_persists_severe(self):
        """Quality drops severely with sustained spam."""
        for _ in range(25):
            adb.log_activity(1, 100, "sticker")
        q = adb._detect_spam(1, 100, "sticker")
        assert q < 0.2  # severe penalty


# ---- activity logging tests ------------------------------------------------


class TestLogActivity:
    def test_first_activity_creates_entry(self):
        """First activity for a new user creates an mmr entry at 1500."""
        delta = adb.log_activity(1, 100, "voice_vad", duration_secs=30)
        mmr = _get_mmr(1, 100)
        assert mmr is not None
        assert mmr["rating"] == pytest.approx(1500, abs=50)
        assert mmr["total_activities"] == 1

    def test_activity_appears_in_log(self):
        """Activity shows up in the log table."""
        adb.log_activity(1, 100, "message", value=1)
        assert _count_rows("activity_log") == 1

    def test_high_value_activity_boosts_rating(self):
        """High-quality activity increases MMR."""
        delta = adb.log_activity(
            1,
            100,
            "forum_create",
            value=1,
            quality_score=1.0,
        )
        mmr = _get_mmr(1, 100)
        assert mmr["rating"] > 1500

    def test_premium_penalty(self):
        """Premium users get a 0.85 multiplier on quality."""
        normal = adb.log_activity(1, 100, "message", value=1, is_premium=False)
        premium = adb.log_activity(2, 100, "message", value=1, is_premium=True)
        # Premium should have same or lower impact
        mmr_norm = _get_mmr(1, 100)
        mmr_prem = _get_mmr(2, 100)
        # Starting from same 1500, identical activity with premium penalty
        # should yield lower (or not higher) rating
        assert mmr_prem["rating"] <= mmr_norm["rating"] + 1

    def test_diff_guilds_independent(self):
        """MMR is tracked per-guild."""
        adb.log_activity(1, 100, "voice_vad", duration_secs=60)
        adb.log_activity(1, 200, "voice_vad", duration_secs=10)
        mmr_a = _get_mmr(1, 100)
        mmr_b = _get_mmr(1, 200)
        assert mmr_a is not None
        assert mmr_b is not None


# ---- query tests -----------------------------------------------------------


class TestQueries:
    def test_user_stats_returns_breakdown(self):
        adb.log_activity(1, 100, "voice_vad", duration_secs=30)
        adb.log_activity(1, 100, "thread_post", value=1)
        stats = _get_mmr(1, 100)
        assert stats is not None
        assert "recent_activities" in stats
        assert len(stats["recent_activities"]) >= 2

    def test_leaderboard_orders_by_rating(self):
        adb.log_activity(1, 100, "forum_create", value=1)
        adb.log_activity(2, 100, "message", value=1)
        lb = adb.get_leaderboard(100)
        assert len(lb) >= 2
        assert lb[0]["rating"] >= lb[1]["rating"]

    def test_leaderboard_respects_limit(self):
        for uid in range(1, 11):
            adb.log_activity(uid, 100, "message", value=1)
        lb = adb.get_leaderboard(100, limit=5)
        assert len(lb) == 5

    def test_get_all_data_returns_full_snapshot(self):
        adb.log_activity(1, 100, "message", value=1)
        data = adb.get_all_data()
        assert "mmr" in data
        assert "activity" in data
        assert "daily" in data
        assert "config" in data
        assert len(data["activity"]) >= 1


# ---- config tests ----------------------------------------------------------


class TestConfig:
    def test_get_weight(self):
        w = adb._get_weight("message")
        assert w == 0.3

    def test_set_weight(self):
        adb.set_config("weight_message", "0.5")
        assert adb._get_weight("message") == 0.5

    def test_custom_weights_reflected_in_mmr(self):
        """Changing a weight changes the MMR impact."""
        adb.log_activity(1, 100, "thread_post", value=1)
        delta_before = _get_mmr(1, 100)["rating"]
        adb.set_config("weight_thread_post", "10.0")
        adb.log_activity(1, 100, "thread_post", value=1)
        delta_after = _get_mmr(1, 100)["rating"]
        assert delta_after > delta_before


# ---- premium tracking tests ------------------------------------------------


class TestPremium:
    def test_set_premium(self):
        adb.log_activity(1, 100, "message")
        adb.set_premium(1, 100, True)
        users = adb.get_premium_users()
        assert 1 in users

    def test_premium_not_in_list(self):
        adb.log_activity(1, 100, "message")
        assert 1 not in adb.get_premium_users()


# ---- idle decay tests ------------------------------------------------------


class TestDecay:
    def test_long_inactivity_increases_deviation(self, monkeypatch):
        monkeypatch.setattr(adb, "_now", lambda: int(time.time()) - 86400 * 5)
        adb.log_activity(1, 100, "message", value=1)
        monkeypatch.setattr(adb, "_now", time.time)
        # Activity after 5 days → decay should have expanded deviation
        adb.log_activity(1, 100, "message", value=1)
        mmr = _get_mmr(1, 100)
        assert mmr is not None
        # rd decay per day is 10, over 5 days that's +50 to rd
        assert mmr["deviation"] > 30  # wouldn't be at min after decay

    def test_rating_drifts_toward_1500_with_decay(self, monkeypatch):
        adb.set_config("decay_rating_per_day", "5")
        # Push rating up first
        adb.log_activity(1, 100, "forum_create", value=1)
        adb.log_activity(1, 100, "forum_create", value=1)
        mmr = _get_mmr(1, 100)
        high = mmr["rating"]
        # Now jump forward 10 days
        monkeypatch.setattr(adb, "_now", lambda: int(time.time()) + 86400 * 10)
        adb.log_activity(1, 100, "message", value=1)
        mmr = _get_mmr(1, 100)
        assert mmr["rating"] < high
