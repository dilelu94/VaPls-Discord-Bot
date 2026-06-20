import os
import json
from pathlib import Path
import pytest
import storyManager


@pytest.fixture
def temp_state_file(tmp_path, monkeypatch):
    state_file = tmp_path / "story_state.json"
    monkeypatch.setattr(storyManager, "_STATE_FILE", str(state_file))
    return state_file


def test_state_flush_and_recover(temp_state_file):
    # Clear initial state
    storyManager._stories_today.clear()
    storyManager._story_date = ""
    storyManager._last_story_at.clear()
    storyManager._last_chat_activity.clear()
    storyManager._messages_since_story.clear()
    storyManager._last_voice_trigger.clear()

    # Populate state with sample data
    storyManager._stories_today[123456] = 2
    storyManager._story_date = "2026-06-20"
    storyManager._last_story_at[123456] = 1718910000.0
    storyManager._last_chat_activity[123456] = 1718912000.0
    storyManager._messages_since_story[123456] = 4
    storyManager._last_voice_trigger[123456] = 1718913000.0

    # Flush state to temporary file
    storyManager._state_flush()

    assert temp_state_file.exists()

    # Load and inspect JSON directly
    raw_data = json.loads(temp_state_file.read_text())
    assert raw_data["story_date"] == "2026-06-20"
    assert raw_data["stories_today"]["123456"] == 2
    assert raw_data["last_story_at"]["123456"] == 1718910000.0

    # Clear in-memory variables to simulate restart
    storyManager._stories_today.clear()
    storyManager._story_date = ""
    storyManager._last_story_at.clear()
    storyManager._last_chat_activity.clear()
    storyManager._messages_since_story.clear()
    storyManager._last_voice_trigger.clear()

    # Recover state
    storyManager._recover_state()

    # Assert correct types and values are restored
    assert storyManager._story_date == "2026-06-20"
    assert storyManager._stories_today[123456] == 2
    assert storyManager._last_story_at[123456] == 1718910000.0
    assert storyManager._last_chat_activity[123456] == 1718912000.0
    assert storyManager._messages_since_story[123456] == 4
    assert storyManager._last_voice_trigger[123456] == 1718913000.0
