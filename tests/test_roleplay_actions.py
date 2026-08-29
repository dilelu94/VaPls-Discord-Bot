import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import config
import geminiCommand


# ---------------------------------------------------------------------------
# 1. Main bot relay helpers tests (_relay_leave_to_userbot & _relay_join_to_userbot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_leave_to_userbot_disabled(monkeypatch):
    """Returns False when relay URL or secret is missing."""
    monkeypatch.setattr(config, "INDIO_RELAY_URL", None, raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", None, raising=False)
    ok = await geminiCommand._relay_leave_to_userbot(123)
    assert ok is False


@pytest.mark.asyncio
async def test_relay_leave_to_userbot_success(monkeypatch):
    """POSTs to /leave endpoint with X-API-Secret and returns boolean result."""
    posted_url = None
    posted_payload = None

    class MockResponse:
        status = 200
        async def json(self, content_type=None):
            return {"left": True, "guild_id": 123}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self, *args, **kwargs):
            pass
        def post(self, url, json=None, headers=None, timeout=None):
            nonlocal posted_url, posted_payload
            posted_url = url
            posted_payload = json
            return MockResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://localhost:8081", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "testsecret", raising=False)
    monkeypatch.setattr(aiohttp, "ClientSession", MockSession)

    ok = await geminiCommand._relay_leave_to_userbot(123)
    assert ok is True
    assert posted_url == "http://localhost:8081/leave"
    assert posted_payload == {"guild_id": 123}


@pytest.mark.asyncio
async def test_relay_join_to_userbot_success(monkeypatch):
    """POSTs to /join endpoint with X-API-Secret and returns boolean result."""
    posted_url = None
    posted_payload = None

    class MockResponse:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self, *args, **kwargs):
            pass
        def post(self, url, json=None, headers=None, timeout=None):
            nonlocal posted_url, posted_payload
            posted_url = url
            posted_payload = json
            return MockResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(config, "INDIO_RELAY_URL", "http://localhost:8081", raising=False)
    monkeypatch.setattr(config, "INDIO_RELAY_SECRET", "testsecret", raising=False)
    monkeypatch.setattr(aiohttp, "ClientSession", MockSession)

    ok = await geminiCommand._relay_join_to_userbot(456)
    assert ok is True
    assert posted_url == "http://localhost:8081/join"
    assert posted_payload == {"channel_id": 456}


# ---------------------------------------------------------------------------
# 2. Function calls translation tests (_actions_from_function_calls)
# ---------------------------------------------------------------------------


def test_actions_from_function_calls_roleplay():
    """Translates Gemini disconnect_indio and troll_move_user tool calls correctly."""
    calls = [
        {"name": "disconnect_indio", "args": {"duration_seconds": 15}},
        {"name": "troll_move_user", "args": {"target_user": "Miles"}},
    ]
    actions = geminiCommand._actions_from_function_calls(calls)
    assert len(actions) == 2
    assert actions[0] == ("DISCONNECT_INDIO", '{"duration_seconds": 15}')
    assert actions[1] == ("TROLL_MOVE_USER", "Miles")


# ---------------------------------------------------------------------------
# 3. DISCONNECT_INDIO action dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_disconnect_indio_success(monkeypatch):
    """DISCONNECT_INDIO triggers relay leave and schedules join after duration."""
    monkeypatch.setattr(geminiCommand, "_relay_leave_to_userbot", AsyncMock(return_value=True))
    monkeypatch.setattr(geminiCommand, "_relay_join_to_userbot", AsyncMock(return_value=True))

    mock_bot = MagicMock()
    mock_bot.user.id = 999
    mock_guild = MagicMock(id=100)

    mock_voice_ch = MagicMock(id=456)
    mock_member = MagicMock(id=999)
    mock_voice_ch.members = [mock_member]
    mock_guild.voice_channels = [mock_voice_ch]
    mock_bot.get_guild.return_value = mock_guild

    statuses = await geminiCommand._dispatch_indio_actions(
        mock_bot, 100, [("DISCONNECT_INDIO", '{"duration_seconds": 5}')]
    )

    assert statuses == ["disconnect: ok"]
    geminiCommand._relay_leave_to_userbot.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_dispatch_disconnect_indio_not_in_voice(monkeypatch):
    """DISCONNECT_INDIO returns 'disconnect: not in voice' if bot is not in voice."""
    monkeypatch.setattr(geminiCommand, "_relay_leave_to_userbot", AsyncMock(return_value=False))

    mock_bot = MagicMock()
    mock_guild = MagicMock(id=100)
    mock_guild.voice_channels = []
    mock_bot.get_guild.return_value = mock_guild
    mock_bot.voice_clients = []

    statuses = await geminiCommand._dispatch_indio_actions(
        mock_bot, 100, [("DISCONNECT_INDIO", "")]
    )

    assert statuses == ["disconnect: not in voice"]


# ---------------------------------------------------------------------------
# 4. TROLL_MOVE_USER action dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_troll_move_user_success(monkeypatch):
    """TROLL_MOVE_USER moves target user between voice channels when permissions exist."""
    mock_bot = MagicMock()
    mock_guild = MagicMock(id=100)

    mock_vc1 = MagicMock(id=1, name="Canal 1")
    mock_vc2 = MagicMock(id=2, name="Canal 2")
    mock_guild.voice_channels = [mock_vc1, mock_vc2]

    mock_member = MagicMock(id=55, display_name="Viny", name="viny")
    mock_member.voice.channel = mock_vc1
    mock_member.move_to = AsyncMock()
    mock_guild.members = [mock_member]

    mock_bot.get_guild.return_value = mock_guild

    # Permissions mock
    mock_perms = MagicMock(move_members=True)
    mock_vc1.permissions_for.return_value = mock_perms

    statuses = await geminiCommand._dispatch_indio_actions(
        mock_bot, 100, [("TROLL_MOVE_USER", "Viny")], requester_member=mock_member
    )

    assert statuses == ["move: ok"]


@pytest.mark.asyncio
async def test_dispatch_troll_move_user_target_not_in_voice(monkeypatch):
    """TROLL_MOVE_USER returns 'move: target not in voice' if user is not in voice."""
    mock_bot = MagicMock()
    mock_guild = MagicMock(id=100)
    mock_member = MagicMock(id=55, display_name="Viny", voice=None)
    mock_guild.members = [mock_member]
    mock_bot.get_guild.return_value = mock_guild

    statuses = await geminiCommand._dispatch_indio_actions(
        mock_bot, 100, [("TROLL_MOVE_USER", "Viny")], requester_member=mock_member
    )

    assert statuses == ["move: target not in voice"]


@pytest.mark.asyncio
async def test_dispatch_troll_move_user_no_other_channel(monkeypatch):
    """TROLL_MOVE_USER returns 'move: no other channel' if only 1 voice channel exists."""
    mock_bot = MagicMock()
    mock_guild = MagicMock(id=100)
    mock_vc1 = MagicMock(id=1, name="Único Canal")
    mock_guild.voice_channels = [mock_vc1]

    mock_member = MagicMock(id=55, display_name="Viny")
    mock_member.voice.channel = mock_vc1
    mock_guild.members = [mock_member]
    mock_bot.get_guild.return_value = mock_guild

    statuses = await geminiCommand._dispatch_indio_actions(
        mock_bot, 100, [("TROLL_MOVE_USER", "requester")], requester_member=mock_member
    )

    assert statuses == ["move: no other channel"]


@pytest.mark.asyncio
async def test_dispatch_troll_move_user_missing_permissions(monkeypatch):
    """TROLL_MOVE_USER returns 'move: missing permissions' if bot lacks move_members perm."""
    mock_bot = MagicMock()
    mock_guild = MagicMock(id=100)
    mock_vc1 = MagicMock(id=1, name="Canal 1")
    mock_vc2 = MagicMock(id=2, name="Canal 2")
    mock_guild.voice_channels = [mock_vc1, mock_vc2]

    mock_member = MagicMock(id=55, display_name="Viny")
    mock_member.voice.channel = mock_vc1
    mock_guild.members = [mock_member]
    mock_bot.get_guild.return_value = mock_guild

    # Permissions mock: move_members = False
    mock_perms = MagicMock(move_members=False)
    mock_vc1.permissions_for.return_value = mock_perms

    statuses = await geminiCommand._dispatch_indio_actions(
        mock_bot, 100, [("TROLL_MOVE_USER", "requester")], requester_member=mock_member
    )

    assert statuses == ["move: missing permissions"]
