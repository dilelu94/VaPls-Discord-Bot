"""Stream track selection view for Discord slash commands."""

from __future__ import annotations

import logging
from typing import Callable, Optional

import discord

from media_inspector import MediaTracksInfo

log = logging.getLogger(__name__)


class AudioTrackSelect(discord.ui.Select):
    def __init__(self, tracks_info: MediaTracksInfo):
        options = []
        for track in tracks_info.audio_tracks[:25]:  # Discord limit: 25 options
            options.append(
                discord.SelectOption(
                    label=track.display_name[:100],
                    value=str(track.index),
                    description=f"Idioma: {track.language.upper()} ({track.codec.upper()})",
                    default=(track.index == 0),
                )
            )
        super().__init__(
            placeholder="🔊 Seleccioná la pista de audio (idioma)...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_audio_track = int(self.values[0])
        # Mark selected default option
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(view=self.view)


class SubtitleTrackSelect(discord.ui.Select):
    def __init__(self, tracks_info: MediaTracksInfo):
        options = [
            discord.SelectOption(
                label="🚫 Sin subtítulos",
                value="-1",
                description="No mostrar subtítulos",
                default=True,
            )
        ]
        for track in tracks_info.subtitle_tracks[:24]:  # max 24 + 1 = 25
            options.append(
                discord.SelectOption(
                    label=track.display_name[:100],
                    value=str(track.index),
                    description=f"Idioma: {track.language.upper()} ({track.codec.upper()})",
                    default=False,
                )
            )
        super().__init__(
            placeholder="💬 Seleccioná la pista de subtítulos...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_subtitle_track = int(self.values[0])
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(view=self.view)


class StreamTrackSelectView(discord.ui.View):
    def __init__(
        self,
        tracks_info: MediaTracksInfo,
        on_start_callback: Callable[[discord.Interaction, int, int], None],
        timeout: float = 60.0,
    ):
        super().__init__(timeout=timeout)
        self.tracks_info = tracks_info
        self.on_start_callback = on_start_callback
        self.selected_audio_track: int = 0
        self.selected_subtitle_track: int = -1

        if len(tracks_info.audio_tracks) > 1:
            self.add_item(AudioTrackSelect(tracks_info))

        if len(tracks_info.subtitle_tracks) > 0:
            self.add_item(SubtitleTrackSelect(tracks_info))

    @discord.ui.button(label="▶ Transmitir en Go Live", style=discord.ButtonStyle.success, row=2)
    async def start_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="⏳ Iniciando transmisión con la configuración seleccionada...",
            view=self,
        )
        try:
            await self.on_start_callback(
                interaction, self.selected_audio_track, self.selected_subtitle_track
            )
        except Exception as e:
            log.exception("Error in stream track selection start callback")
            await interaction.followup.send(f"❌ Error iniciando transmisión: {e}", ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
