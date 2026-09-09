"""
Tests for chat_db.py module.
"""

import time
import pytest
import chat_db


@pytest.fixture(autouse=True)
def setup_temp_db(tmp_path):
    db_file = str(tmp_path / "test_chat.db")
    chat_db.init_db(db_file)
    yield
    chat_db.close_db()


def test_save_and_search_messages():
    now = int(time.time())
    messages = [
        {
            "message_id": 101,
            "guild_id": 1,
            "channel_id": 10,
            "channel_name": "soreteposting",
            "author_id": 50,
            "author_name": "Seba",
            "content": "La IP del servidor de Valheim es 163.176.114.14:2456",
            "created_at": now - 100,
        },
        {
            "message_id": 102,
            "guild_id": 1,
            "channel_id": 10,
            "channel_name": "soreteposting",
            "author_id": 51,
            "author_name": "Miles",
            "content": "Agreguen el server a favoritos",
            "created_at": now - 50,
        },
        {
            "message_id": 103,
            "guild_id": 1,
            "channel_id": 20,
            "channel_name": "general",
            "author_id": 50,
            "author_name": "Seba",
            "content": "Nos vemos mas tarde para jugar",
            "created_at": now,
        },
    ]

    saved = chat_db.save_messages_batch(messages)
    assert saved == 3

    # Search for Valheim
    res = chat_db.search_messages("Valheim")
    assert len(res) == 1
    assert res[0]["message_id"] == 101
    assert "163.176.114.14" in res[0]["content"]

    # Filter by author
    res_author = chat_db.search_messages("jugar", author_name="Seba")
    assert len(res_author) == 1
    assert res_author[0]["author_name"] == "Seba"

    # Filter by channel
    res_chan = chat_db.search_messages("", channel_name="soreteposting")
    assert len(res_chan) == 2


def test_scrape_progress_tracking():
    chan_id = 12345
    progress = chat_db.get_scrape_progress(chan_id)
    assert progress is None

    chat_db.update_scrape_progress(
        channel_id=chan_id,
        guild_id=1,
        channel_name="test-channel",
        oldest_message_id=999,
        added_count=100,
        completed=False,
    )

    p1 = chat_db.get_scrape_progress(chan_id)
    assert p1 is not None
    assert p1["messages_scraped"] == 100
    assert p1["oldest_message_id"] == 999
    assert p1["completed"] == 0

    chat_db.mark_scrape_completed(chan_id)
    p2 = chat_db.get_scrape_progress(chan_id)
    assert p2["completed"] == 1


def test_mark_message_deleted():
    msg = {
        "message_id": 201,
        "guild_id": 1,
        "channel_id": 10,
        "channel_name": "general",
        "author_id": 50,
        "author_name": "Seba",
        "content": "Mensaje secreto",
        "created_at": int(time.time()),
    }
    chat_db.save_message(msg)

    res_before = chat_db.search_messages("secreto")
    assert len(res_before) == 1
    assert res_before[0]["is_deleted"] == 0

    ok = chat_db.mark_message_deleted(201)
    assert ok is True

    res_after = chat_db.search_messages("secreto")
    assert len(res_after) == 1
    assert res_after[0]["is_deleted"] == 1

