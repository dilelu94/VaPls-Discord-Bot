"""Unit tests for SoundpadView navigation and playback logic."""
import os
import shutil
import unittest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock, PropertyMock
import discord

import config
from soundpadCommand import SoundpadView, soundpadLogic

class MockVC:
    """Minimal voice client stub used for Soundpad tests."""
    def __init__(self):
        self.played = None
        self.isPlayingState = False
        self.isPausedState = False
        self.channel = MagicMock()
        self.channel.id = 1111

    def is_connected(self):
        return True

    def is_playing(self):
        return self.isPlayingState

    def is_paused(self):
        return self.isPausedState

    def stop(self):
        self.isPlayingState = False
        self.isPausedState = False

    def play(self, source, after=None):
        self.played = source
        self.isPlayingState = True
        self.isPausedState = False

class TestSoundpadSubfoldersAndPagination(unittest.IsolatedAsyncioTestCase):
    """Async test suite covering Soundpad navigation and playback."""
    async def asyncSetUp(self):
        self.temp_audio_dir = "/tmp/test_soundpad_navigation"
        os.makedirs(self.temp_audio_dir, exist_ok=True)
        
        # Create nested folders
        os.makedirs(os.path.join(self.temp_audio_dir, "Audios"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_audio_dir, "Audios/Quandale Dingle"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_audio_dir, "Audios/Quandale Dingle/sub"), exist_ok=True)
        os.makedirs(os.path.join(self.temp_audio_dir, "Mila"), exist_ok=True)
        
        # Create dummy sound files
        with open(os.path.join(self.temp_audio_dir, "Audios/sound1.mp3"), "w") as f:
            f.write("dummy")
        with open(os.path.join(self.temp_audio_dir, "Audios/Quandale Dingle/quandale.mp3"), "w") as f:
            f.write("dummy")
        with open(os.path.join(self.temp_audio_dir, "Audios/Quandale Dingle/sub/nested.m4a"), "w") as f:
            f.write("dummy")
            
        # Create 27 files in Mila to test pagination (> 25 files)
        for i in range(27):
            with open(os.path.join(self.temp_audio_dir, f"Mila/mila_{i:02d}.wav"), "w") as f:
                f.write("dummy")
            
        # Patch configurations
        self.patcher1 = patch.object(config, "CUSTOM_AUDIO_PATH", self.temp_audio_dir)
        self.patcher2 = patch.object(config, "AUDIO_DIR", self.temp_audio_dir)
        self.patcher1.start()
        self.patcher2.start()
        
        self.vc = MockVC()
        
        # Mock Context
        self.ctx = MagicMock(spec=discord.ApplicationContext)
        self.ctx.bot = MagicMock()
        self.ctx.guild = MagicMock()
        self.ctx.guild.id = 12345
        self.ctx.channel = MagicMock()
        self.ctx.author.voice = MagicMock()
        self.ctx.author.voice.channel = MagicMock()
        self.ctx.author.voice.channel.id = 1111
        self.ctx.voice_client = self.vc
        self.ctx.respond = AsyncMock()
        self.ctx.response = MagicMock()
        self.ctx.response.is_done = MagicMock(return_value=False)
        self.ctx.defer = AsyncMock()
        self.ctx.followup = MagicMock()
        self.ctx.followup.send = AsyncMock()
        
        # Mock Interaction
        self.interaction = MagicMock(spec=discord.Interaction)
        self.interaction.guild = self.ctx.guild
        self.interaction.user = self.ctx.author
        self.interaction.guild.voice_client = self.vc
        self.interaction.response = MagicMock()
        self.interaction.response.defer = AsyncMock()
        self.interaction.response.is_done = MagicMock(return_value=False)
        self.interaction.response.edit_message = AsyncMock()
        self.interaction.edit_original_response = AsyncMock()
        self.interaction.followup = MagicMock()
        self.interaction.followup.send = AsyncMock()

    async def asyncTearDown(self):
        self.patcher1.stop()
        self.patcher2.stop()
        if os.path.exists(self.temp_audio_dir):
            shutil.rmtree(self.temp_audio_dir)

    async def test_subfolder_discovery(self):
        view = SoundpadView(self.temp_audio_dir)
        subfolders = view.get_subfolders("Audios")
        # Should detect root and the two subfolders
        self.assertEqual(subfolders, ["/", "Quandale Dingle", "Quandale Dingle/sub"])

    async def test_get_folder_files_segmentation(self):
        view = SoundpadView(self.temp_audio_dir)
        
        # Files at root "/" of Audios (only sound1.mp3, excluding subfolders)
        root_files = view.get_folder_files("Audios", "/")
        self.assertEqual(root_files, ["sound1.mp3"])
        
        # Files in "Quandale Dingle" subfolder
        quandale_files = view.get_folder_files("Audios", "Quandale Dingle")
        self.assertEqual(quandale_files, ["Quandale Dingle/quandale.mp3"])
        
        # Files in "Quandale Dingle/sub" subfolder
        nested_files = view.get_folder_files("Audios", "Quandale Dingle/sub")
        self.assertEqual(nested_files, ["Quandale Dingle/sub/nested.m4a"])

    async def test_subfolder_selection_callback(self):
        view = SoundpadView(self.temp_audio_dir)
        
        # Select "Quandale Dingle" subfolder
        self.interaction.data = {"values": ["Quandale Dingle"]}
        await view.on_subfolder_select(self.interaction)
        
        self.assertEqual(view.selected_subfolder, "Quandale Dingle")
        self.assertEqual(view.selected_file, "Quandale Dingle/quandale.mp3")
        self.interaction.response.edit_message.assert_called_once()

    async def test_audio_pagination(self):
        # Mila has 27 files, which requires 2 pages (25 + 2)
        view = SoundpadView(self.temp_audio_dir)
        
        # Select Mila category
        self.interaction.data = {"values": ["Mila"]}
        await view.on_category_select(self.interaction)
        
        self.assertEqual(view.selected_category, "Mila")
        self.assertEqual(view.selected_subfolder, "/")
        self.assertEqual(view.total_pages, 2)
        self.assertEqual(view.current_page, 0)
        
        # First page should contain 25 files
        self.assertEqual(len(view.files_by_index), 25)
        self.assertEqual(view.files_by_index["0"], "mila_00.wav")
        self.assertEqual(view.files_by_index["24"], "mila_24.wav")
        
        # Click Next page
        await view.on_next_click(self.interaction)
        self.assertEqual(view.current_page, 1)
        
        # Second page should contain 2 files
        self.assertEqual(len(view.files_by_index), 2)
        self.assertEqual(view.files_by_index["0"], "mila_25.wav")
        self.assertEqual(view.files_by_index["1"], "mila_26.wav")
        
        # Click Previous page
        await view.on_prev_click(self.interaction)
        self.assertEqual(view.current_page, 0)
        self.assertEqual(len(view.files_by_index), 25)

    @patch("discord.FFmpegOpusAudio")
    async def test_playback_resolves_nested_paths_correctly(self, mockFfmpeg):
        mockFfmpeg.return_value = MagicMock()
        view = SoundpadView(self.temp_audio_dir)
        
        # Manually select a nested file
        view.selected_file = "Quandale Dingle/sub/nested.m4a"
        await view.play_sound(self.interaction)
        expected_path = os.path.join(self.temp_audio_dir, "Audios", "Quandale Dingle/sub/nested.m4a")
        # Only the resolved path is the behavior under test; FFmpeg options
        # (e.g. dynaudnorm normalization) may be passed but are unrelated.
        mockFfmpeg.assert_called_once()
        self.assertEqual(mockFfmpeg.call_args.args[0], expected_path)
        self.assertTrue(self.vc.is_playing())

    @patch("playCommand.guildPlayers")
    async def test_soundpad_logic_rejection_if_music_playing(self, mock_guild_players):
        mock_player = MagicMock()
        mock_player.currentSong = {"id": "song123", "title": "A cool song"}
        mock_guild_players.__contains__.return_value = True
        mock_guild_players.__getitem__.return_value = mock_player
        
        await soundpadLogic(self.ctx)
        
        self.ctx.followup.send.assert_called_once_with(
            "⚠️ El bot está reproduciendo música. Por favor, detén la música antes de usar el Soundpad.", 
            ephemeral=True
        )

    @patch("playCommand.guildPlayers")
    async def test_play_sound_rejection_if_music_playing(self, mock_guild_players):
        mock_player = MagicMock()
        mock_player.currentSong = {"id": "song123", "title": "A cool song"}
        mock_guild_players.__contains__.return_value = True
        mock_guild_players.__getitem__.return_value = mock_player
        
        view = SoundpadView(self.temp_audio_dir)
        view.selected_file = "sound1.mp3"
        
        await view.play_sound(self.interaction)
        
        self.interaction.followup.send.assert_called_once_with(
            "⚠️ El bot está reproduciendo música. Por favor, detén la música antes de usar el Soundpad.", 
            ephemeral=True
        )
        self.assertFalse(self.vc.is_playing())

if __name__ == "__main__":
    unittest.main()
