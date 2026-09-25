"""Behavioral tests for GET /activity/last-voice endpoint in apiServer.

Verifies that if guild.fetch_members raises RuntimeError (e.g. Session is closed during disconnect/shutdown),
the endpoint catches the exception, logs a warning, falls back to guild.members, and returns 200 OK.
"""
from __future__ import annotations

import types
import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
from apiServer import makeApp

API_SECRET = "test-secret"
HEADERS = {"X-API-Secret": API_SECRET}


class _FakeRole:
    def __init__(self, name: str, role_id: int):
        self.name = name
        self.id = role_id


class _FakeMember:
    def __init__(self, user_id: int, name: str, roles: list[_FakeRole]):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.roles = roles
        self.bot = False
        self.voice = None


class _FakeGuild:
    def __init__(self, guild_id: int, members: list[_FakeMember], roles: list[_FakeRole]):
        self.id = guild_id
        self.members = members
        self.roles = roles

    def fetch_members(self, limit=None):
        class _FailingAsyncIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("Session is closed")

        return _FailingAsyncIterator()


class _FakeBot:
    def __init__(self, guild: _FakeGuild):
        self.guild = guild
        self.voice_clients = []

    def get_guild(self, guild_id):
        if guild_id == self.guild.id:
            return self.guild
        return None


@pytest.fixture(autouse=True)
def _api_secret(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET", API_SECRET, raising=False)


async def test_last_voice_falls_back_on_closed_session():
    mc_role = _FakeRole("Main Characters", role_id=101)
    member = _FakeMember(user_id=42, name="mati", roles=[mc_role])
    guild = _FakeGuild(guild_id=100, members=[member], roles=[mc_role])
    bot = _FakeBot(guild)

    app = makeApp(bot)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        resp = await client.get("/last-voice?guild_id=100", headers=HEADERS)
        assert resp.status == 200
        body = await resp.json()
        assert "users" in body
        assert len(body["users"]) == 1
        assert body["users"][0]["id"] == 42
    finally:
        await client.close()
