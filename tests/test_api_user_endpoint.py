"""Behavioral tests for GET /user/<user_id>?guild_id=<gid>.

We pin the user-facing shape consumed by the Telegram /userinfo handler:
- top_role is omitted (None) when the member only has @everyone
- top_role exposes {name, color "#rrggbb"} when there is a real role
- voice is filled via channel-membership fallback even when member.voice is
  None (the bug that made /userinfo report no voice channel for users who
  were actually connected).
"""
from __future__ import annotations

import datetime
import types
from typing import Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
from apiServer import makeApp


# --- fakes ------------------------------------------------------------------

class _FakeColor:
    def __init__(self, value: int) -> None:
        self.value = value


class _FakeRole:
    def __init__(self, name: str, color_value: int = 0) -> None:
        self.name = name
        self.color = _FakeColor(color_value)


class _FakeVoiceState:
    def __init__(self, channel, self_mute: bool = False, self_deaf: bool = False) -> None:
        self.channel = channel
        self.self_mute = self_mute
        self.self_deaf = self_deaf


class _FakeVoiceChannel:
    def __init__(self, channel_id: int, name: str, members: Optional[list] = None) -> None:
        self.id = channel_id
        self.name = name
        self.members = members or []


class _FakeMember:
    def __init__(
        self,
        *,
        user_id: int,
        display_name: str = "Mati",
        name: str = "mati",
        roles: Optional[list[_FakeRole]] = None,
        voice: Optional[_FakeVoiceState] = None,
        top_role: Optional[_FakeRole] = None,
        joined_at: Optional[datetime.datetime] = None,
        created_at: Optional[datetime.datetime] = None,
        status: str = "online",
        activities: Optional[list] = None,
    ) -> None:
        self.id = user_id
        self.display_name = display_name
        self.name = name
        self.roles = roles or [_FakeRole("@everyone")]
        self.voice = voice
        self.top_role = top_role if top_role is not None else self.roles[-1]
        self.joined_at = joined_at
        self.created_at = created_at or datetime.datetime(
            2020, 1, 1, tzinfo=datetime.timezone.utc
        )
        self.status = status
        self.activities = activities or []
        self.bot = False


class _FakeGuild:
    def __init__(self, guild_id: int, members: list[_FakeMember],
                 voice_channels: Optional[list[_FakeVoiceChannel]] = None) -> None:
        self.id = guild_id
        self._members = {m.id: m for m in members}
        self.voice_channels = voice_channels or []

    def get_member(self, user_id):
        return self._members.get(user_id)


class _FakeBot:
    def __init__(self, guild: _FakeGuild) -> None:
        self._guild = guild

    def get_guild(self, guild_id):
        if guild_id == self._guild.id:
            return self._guild
        return None


# --- harness ----------------------------------------------------------------

API_SECRET = "test-secret"
HEADERS = {"X-API-Secret": API_SECRET}


@pytest.fixture(autouse=True)
def _api_secret(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET", API_SECRET, raising=False)


async def _client(bot) -> TestClient:
    app = makeApp(bot)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --- tests ------------------------------------------------------------------

async def test_top_role_is_null_when_member_only_has_everyone():
    member = _FakeMember(
        user_id=42,
        roles=[_FakeRole("@everyone")],
    )
    guild = _FakeGuild(guild_id=100, members=[member])
    client = await _client(_FakeBot(guild))
    try:
        resp = await client.get("/user/42?guild_id=100", headers=HEADERS)
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["top_role"] is None


async def test_top_role_exposes_name_and_hex_color_when_member_has_real_role():
    admin = _FakeRole("Admin", color_value=0xff8800)
    member = _FakeMember(
        user_id=42,
        roles=[_FakeRole("@everyone"), admin],
        top_role=admin,
    )
    guild = _FakeGuild(guild_id=100, members=[member])
    client = await _client(_FakeBot(guild))
    try:
        resp = await client.get("/user/42?guild_id=100", headers=HEADERS)
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["top_role"] == {"name": "Admin", "color": "#ff8800"}


async def test_voice_falls_back_to_channel_members_when_member_voice_is_none():
    # The member's voice state is None (cache race) but they are listed in
    # the channel's members. The endpoint should still report the channel.
    member = _FakeMember(user_id=42, voice=None)
    channel = _FakeVoiceChannel(channel_id=999, name="General",
                                members=[member])
    guild = _FakeGuild(guild_id=100, members=[member],
                       voice_channels=[channel])
    client = await _client(_FakeBot(guild))
    try:
        resp = await client.get("/user/42?guild_id=100", headers=HEADERS)
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["voice"] is not None
    assert body["voice"]["channel_id"] == 999
    assert body["voice"]["channel_name"] == "General"


async def test_voice_is_null_when_member_is_in_no_channel():
    member = _FakeMember(user_id=42, voice=None)
    other = _FakeMember(user_id=7)
    channel = _FakeVoiceChannel(channel_id=999, name="General",
                                members=[other])
    guild = _FakeGuild(guild_id=100, members=[member, other],
                       voice_channels=[channel])
    client = await _client(_FakeBot(guild))
    try:
        resp = await client.get("/user/42?guild_id=100", headers=HEADERS)
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200
    assert body["voice"] is None
