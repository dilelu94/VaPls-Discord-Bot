"""
Tests for chat_scraper.py module.
"""

from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock
import chat_db
import chat_scraper


@pytest.fixture(autouse=True)
def setup_temp_db(tmp_path):
    db_file = str(tmp_path / "test_scraper.db")
    chat_db.init_db(db_file)
    yield
    chat_db.close_db()


def test_should_index_message():
    # Regular user message
    user_msg = MagicMock()
    user_msg.author.bot = False
    user_msg.author.id = 12345
    user_msg.content = "Hola gente"
    user_msg.attachments = []
    assert chat_scraper.should_index_message(user_msg) is True

    # Bot message should be ignored
    bot_msg = MagicMock()
    bot_msg.author.bot = True
    assert chat_scraper.should_index_message(bot_msg) is False

    # Empty message without attachments should be ignored
    empty_msg = MagicMock()
    empty_msg.author.bot = False
    empty_msg.content = "   "
    empty_msg.attachments = []
    assert chat_scraper.should_index_message(empty_msg) is False

    # Message with image link or attachment
    att_msg = MagicMock()
    att_msg.author.bot = False
    att_msg.content = ""
    att_msg.attachments = ["http://example.com/file.jpg"]
    assert chat_scraper.should_index_message(att_msg) is True


@pytest.mark.asyncio
async def test_scrape_channel_history_pagination(monkeypatch):
    monkeypatch.setattr(chat_scraper, "BATCH_DELAY_SECONDS", 0.001)

    now_dt = datetime.now(timezone.utc)
    m1 = MagicMock()
    m1.id = 500
    m1.guild.id = 1
    m1.channel.id = 10
    m1.channel.name = "general"
    m1.author.bot = False
    m1.author.id = 99
    m1.author.display_name = "Chalo"
    m1.content = "Mensaje 1"
    m1.attachments = []
    m1.created_at = now_dt

    m2 = MagicMock()
    m2.id = 499
    m2.guild.id = 1
    m2.channel.id = 10
    m2.channel.name = "general"
    m2.author.bot = False
    m2.author.id = 100
    m2.author.display_name = "Seba"
    m2.content = "Mensaje 2"
    m2.attachments = []
    m2.created_at = now_dt

    async def mock_history_generator(limit=100, before=None, oldest_first=False):
        if before is None:
            for m in [m1, m2]:
                yield m
        # Second call returns nothing to indicate end of channel history

    channel = MagicMock(spec=["id", "guild", "name", "history"])
    channel.id = 10
    channel.guild.id = 1
    channel.name = "general"
    channel.history = mock_history_generator

    added = await chat_scraper.scrape_channel_history(channel)
    assert added == 2

    # Check progress marked as completed
    progress = chat_db.get_scrape_progress(10)
    assert progress["completed"] == 1

    # Search DB for the scraped messages
    results = chat_db.search_messages("Mensaje")
    assert len(results) == 2
