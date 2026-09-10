"""
Tests for chat_db.py module.
"""

import time
from unittest.mock import MagicMock
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


def test_total_messages_indexed():
    assert chat_db.total_messages_indexed() == 0
    now = int(time.time())
    chat_db.save_messages_batch([
        {"message_id": 1, "guild_id": 1, "channel_id": 1, "channel_name": "x",
         "author_id": 1, "author_name": "u", "content": "a", "created_at": now},
        {"message_id": 2, "guild_id": 1, "channel_id": 1, "channel_name": "x",
         "author_id": 1, "author_name": "u", "content": "b", "created_at": now},
    ])
    assert chat_db.total_messages_indexed() == 2


def test_should_index_message_regular_user():
    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = 12345
    msg.content = "Hola gente"
    msg.attachments = []
    assert chat_db.should_index_message(msg) is True


def test_should_index_message_bot_excluded():
    msg = MagicMock()
    msg.author.bot = True
    assert chat_db.should_index_message(msg) is False


def test_should_index_message_empty_no_attachments():
    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = 12345
    msg.content = "   "
    msg.attachments = []
    assert chat_db.should_index_message(msg) is False


def test_should_index_message_with_attachment_no_text():
    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = 12345
    msg.content = ""
    msg.attachments = ["http://example.com/file.jpg"]
    assert chat_db.should_index_message(msg) is True


def test_format_message_dict():
    from datetime import datetime, timezone
    now_dt = datetime.now(timezone.utc)
    msg = MagicMock()
    msg.id = 9999
    msg.guild.id = 1
    msg.channel.id = 10
    msg.channel.name = "soreteposting"
    msg.author.bot = False
    msg.author.id = 42
    msg.author.display_name = "Seba"
    msg.content = "probando"
    msg.attachments = []
    msg.created_at = now_dt

    d = chat_db.format_message_dict(msg)
    assert d["message_id"] == 9999
    assert d["author_name"] == "Seba"
    assert d["channel_name"] == "soreteposting"
    assert d["content"] == "probando"
    assert isinstance(d["created_at"], int)
