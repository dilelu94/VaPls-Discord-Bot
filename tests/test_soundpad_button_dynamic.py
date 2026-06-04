import pytest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from soundpadCommand import SoundpadView, SoundpadStopView

@pytest.mark.asyncio
async def test_soundpad_view_stop_button_logic():
    # Setup mock interaction and guild
    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = 123
    mock_interaction = AsyncMock(spec=discord.Interaction)
    mock_interaction.guild = mock_guild
    mock_interaction.response = AsyncMock()
    mock_interaction.user.display_name = "TestUser"
    
    # Mock voice client
    mock_vc = MagicMock()
    mock_vc.is_connected.return_value = True
    mock_vc.is_playing.return_value = False
    mock_guild.voice_client = mock_vc
    
    # Mock output_dir and files
    with patch("os.path.exists", return_value=True),          patch("os.path.isdir", return_value=True),          patch("os.listdir", return_value=["cat1"]),          patch("soundpadCommand.SoundpadView.get_subfolders", return_value=["/"]),          patch("soundpadCommand.SoundpadView.get_folder_files", return_value=["sound1.mp3"]):
        
        view = SoundpadView(output_dir="/mock/audio", guild_id=123)
        view.message = AsyncMock()
        
        # Initial state: stop button should NOT be in items
        stop_buttons = [item for item in view.children if item.custom_id == "btn_sp_stop"]
        assert len(stop_buttons) == 0
        
        # Simulate playing state
        view.is_playing = True
        view.setup_components()
        stop_buttons_playing = [item for item in view.children if item.custom_id == "btn_sp_stop"]
        assert len(stop_buttons_playing) == 1

@pytest.mark.asyncio
async def test_soundpad_stop_view_removal_in_logic():
    # This would test soundpadLogic's behavior after play_clip_by_query
    pass
