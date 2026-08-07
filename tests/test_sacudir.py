"""Behavior tests for /sacudir slash command in bot.py."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import config


@pytest.fixture(autouse=True)
def _reset_sacudir_cooldowns():
    bot._sacudir_cooldowns.clear()
    yield
    bot._sacudir_cooldowns.clear()


class _FakeRole:
    def __init__(self, name: str, position: int):
        self.name = name
        self.position = position

    def __lt__(self, other):
        return self.position < getattr(other, "position", 0)

    def __le__(self, other):
        return self.position <= getattr(other, "position", 0)

    def __gt__(self, other):
        return self.position > getattr(other, "position", 0)

    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)


def _make_member(user_id: int, top_role_pos: int = 1, is_admin: bool = False):
    m = MagicMock(name=f"Member_{user_id}")
    m.id = user_id
    m.mention = f"<@{user_id}>"
    m.top_role = _FakeRole("Role", top_role_pos)
    m.guild_permissions = SimpleNamespace(administrator=is_admin)
    m.voice = SimpleNamespace(channel=SimpleNamespace(id=100))
    m.move_to = AsyncMock()
    return m


def _make_sacudir_ctx(author_id: int, author_role_pos: int = 1, is_admin: bool = False, guild_owner_id: int = 999):
    ctx = MagicMock(name="Ctx")
    ctx.author = _make_member(author_id, author_role_pos, is_admin)
    ctx.guild = MagicMock(name="Guild")
    ctx.guild.owner_id = guild_owner_id
    
    target_vc = MagicMock(spec=["id", "members"])
    target_vc.id = 451581345022476294
    target_vc.members = []
    
    empty_vc = MagicMock(spec=["id", "members"])
    empty_vc.id = 999999
    empty_vc.members = []
    
    members_by_id = {author_id: ctx.author}
    ctx.guild.get_channel = MagicMock(return_value=target_vc)
    ctx.guild.voice_channels = [target_vc, empty_vc]
    ctx.guild.get_member = MagicMock(side_effect=lambda uid: members_by_id.get(uid))
    
    ctx.respond = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup = SimpleNamespace(send=AsyncMock())
    return ctx


async def test_sacudir_allows_bot_owner_more_than_5_times(monkeypatch):
    ctx = _make_sacudir_ctx(author_id=config.OWNER_ID, author_role_pos=1)
    target = _make_member(user_id=12345, top_role_pos=10)  # target has higher role
    
    await bot.sacudir(ctx, usuario=target, veces=20)
    
    # Should not be rejected
    ctx.respond.assert_not_called()
    assert ctx.followup.send.called


async def test_sacudir_allows_guild_owner_more_than_5_times():
    owner_id = 777777
    ctx = _make_sacudir_ctx(author_id=owner_id, author_role_pos=1, guild_owner_id=owner_id)
    target = _make_member(user_id=12345, top_role_pos=10)
    
    await bot.sacudir(ctx, usuario=target, veces=20)
    
    ctx.respond.assert_not_called()
    assert ctx.followup.send.called


async def test_sacudir_allows_admin_over_non_admin():
    ctx = _make_sacudir_ctx(author_id=111, author_role_pos=2, is_admin=True)
    target = _make_member(user_id=222, top_role_pos=5, is_admin=False)  # target higher role but not admin
    
    await bot.sacudir(ctx, usuario=target, veces=10)
    
    ctx.respond.assert_not_called()
    assert ctx.followup.send.called


async def test_sacudir_allows_higher_top_role():
    ctx = _make_sacudir_ctx(author_id=111, author_role_pos=10, is_admin=False)
    target = _make_member(user_id=222, top_role_pos=5, is_admin=False)
    
    await bot.sacudir(ctx, usuario=target, veces=10)
    
    ctx.respond.assert_not_called()
    assert ctx.followup.send.called


async def test_sacudir_blocks_equal_or_lower_role_for_regular_user():
    ctx = _make_sacudir_ctx(author_id=111, author_role_pos=5, is_admin=False, guild_owner_id=999)
    target = _make_member(user_id=222, top_role_pos=5, is_admin=False)
    
    await bot.sacudir(ctx, usuario=target, veces=10)
    
    ctx.respond.assert_called_once()
    assert "rango mayor" in ctx.respond.call_args[0][0]
    ctx.followup.send.assert_not_called()


async def test_sacudir_cooldown_blocks_immediate_repeat():
    ctx = _make_sacudir_ctx(author_id=config.OWNER_ID)
    target = _make_member(user_id=12345)
    
    await bot.sacudir(ctx, usuario=target, veces=5)
    assert ctx.followup.send.called
    
    # Second attempt immediately
    ctx2 = _make_sacudir_ctx(author_id=config.OWNER_ID)
    await bot.sacudir(ctx2, usuario=target, veces=5)
    ctx2.respond.assert_called_once()
    assert "fue sacudido hace poco" in ctx2.respond.call_args[0][0]
