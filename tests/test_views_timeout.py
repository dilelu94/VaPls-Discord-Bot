"""Behavioral and enforcement tests for BaseView and Discord UI button timeouts."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import discord
import pytest

from baseView import BaseView
import soundpadCommand
import playCommand
import bot


@pytest.mark.asyncio
async def test_base_view_on_timeout_edits_message_with_view_none():
    """Pin promise: BaseView.on_timeout removes all buttons from Discord message."""
    view = BaseView(timeout=10)
    btn = discord.ui.Button(label="Test Button", custom_id="btn_test")
    view.add_item(btn)

    mock_msg = AsyncMock(spec=discord.Message)
    view.message = mock_msg

    await view.on_timeout()

    assert len(view.children) == 0
    mock_msg.edit.assert_called_once_with(view=None)


@pytest.mark.asyncio
async def test_base_view_on_timeout_edits_bound_interaction():
    """Pin promise: BaseView.on_timeout edits bound interaction if message is not set."""
    view = BaseView(timeout=10)
    btn = discord.ui.Button(label="Test Button", custom_id="btn_test")
    view.add_item(btn)

    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response.is_done.return_value = True
    mock_interaction.edit_original_response = AsyncMock()
    view.bound_interaction = mock_interaction

    await view.on_timeout()

    assert len(view.children) == 0
    mock_interaction.edit_original_response.assert_called_once_with(view=None)


@pytest.mark.asyncio
async def test_base_view_calls_on_timeout_extra():
    """Pin promise: BaseView.on_timeout executes subclass-defined on_timeout_extra hook."""
    class CustomView(BaseView):
        def __init__(self):
            super().__init__(timeout=10)
            self.extra_called = False

        async def on_timeout_extra(self):
            self.extra_called = True

    view = CustomView()
    await view.on_timeout()
    assert view.extra_called is True


@pytest.mark.asyncio
async def test_soundpad_view_unregisters_and_clears_on_timeout(tmp_path, monkeypatch):
    """Pin promise: SoundpadView unregisters panel and clears buttons on timeout."""
    monkeypatch.setattr(soundpadCommand, "_active_panels", {})

    category = tmp_path / "categoria1"
    category.mkdir()
    (category / "clip.opus").touch()

    view = soundpadCommand.SoundpadView(str(tmp_path), guild_id=12345)
    mock_msg = AsyncMock(spec=discord.Message)
    view.message = mock_msg

    assert soundpadCommand.has_active_panel(12345) is True

    await view.on_timeout()

    assert soundpadCommand.has_active_panel(12345) is False
    assert len(view.children) == 0
    mock_msg.edit.assert_called_once_with(view=None)


def test_all_discord_ui_views_inherit_from_base_view():
    """Enforcement promise: Every discord.ui.View subclass in command modules inherits from BaseView."""
    modules = [bot, playCommand, soundpadCommand]
    view_classes = []

    for mod in modules:
        for name in dir(mod):
            obj = getattr(mod, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, discord.ui.View)
                and obj is not discord.ui.View
                and obj is not BaseView
            ):
                view_classes.append((mod.__name__, obj))

    assert len(view_classes) > 0, "No View classes found to inspect"

    failing = []
    for mod_name, cls in view_classes:
        if not issubclass(cls, BaseView):
            failing.append(f"{mod_name}.{cls.__name__}")

    assert not failing, f"The following View classes do not inherit from BaseView: {failing}"
