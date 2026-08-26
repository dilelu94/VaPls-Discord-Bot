"""Tests for welcome DM sending when a member joins a guild."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import discord


@pytest.mark.asyncio
async def test_on_member_join_sends_dm_with_image():
    from bot import on_member_join

    member = MagicMock(spec=discord.Member)
    member.bot = False
    member.id = 123456789
    member.display_name = "NewMember"
    member.guild = MagicMock()
    member.guild.name = "TestGuild"
    member.send = AsyncMock()

    with patch("os.path.exists", return_value=True), patch("discord.File") as mock_file:
        mock_file.return_value = "mock_file_obj"
        await on_member_join(member)
        member.send.assert_awaited_once_with(file="mock_file_obj")


@pytest.mark.asyncio
async def test_on_member_join_handles_forbidden_dm():
    from bot import on_member_join

    member = MagicMock(spec=discord.Member)
    member.bot = False
    member.id = 123456789
    member.display_name = "BlockedMember"
    member.guild = MagicMock()
    member.guild.name = "TestGuild"
    member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "DMs disabled"))

    with patch("os.path.exists", return_value=True), patch("discord.File"):
        # Should not raise exception
        await on_member_join(member)
