"""Soundpad slash command and UI for playing custom audio clips."""
import os
import asyncio
import discord
import config
import analytics
from greeting import set_pending_trigger

class SoundpadView(discord.ui.View):
    """Interactive UI for browsing and playing soundpad audio files."""
    def __init__(self, output_dir: str):
        """Initialize the soundpad view.

        Args:
            output_dir: Base directory containing category folders with audio.

        Raises:
            ValueError: If the directory is missing or empty.
        """
        super().__init__(timeout=180)
        self.output_dir = output_dir
        self.message = None
        
        if not os.path.exists(output_dir):
            raise ValueError(f"La ruta de audios no existe: {output_dir}")
            
        self.categories = sorted([
            d for d in os.listdir(output_dir) 
            if os.path.isdir(os.path.join(output_dir, d)) and not d.startswith(".")
        ])
        
        if not self.categories:
            raise ValueError("No se encontraron carpetas (categorías) en la ruta de audios.")
            
        self.selected_category = self.categories[0]
        self.selected_subfolder = "/"
        self.current_page = 0
        self.selected_file = None
        self.setup_components()

    def get_subfolders(self, category: str):
        """Return the list of subfolders for a category.

        Args:
            category: Category folder name.

        Returns:
            Sorted list of subfolder paths (including "/").
        """
        cat_dir = os.path.join(self.output_dir, category)
        if not os.path.exists(cat_dir):
            return ["/"]
        subfolders = ["/"]
        for root, dirs, _ in os.walk(cat_dir):
            for d in dirs:
                if not d.startswith("."):
                    full_path = os.path.join(root, d)
                    rel_path = os.path.relpath(full_path, cat_dir)
                    subfolders.append(rel_path)
        return sorted(subfolders)

    def get_folder_files(self, category: str, subfolder: str):
        """List audio files in the selected category/subfolder.

        Args:
            category: Category folder name.
            subfolder: Subfolder path ("/" for root).

        Returns:
            Sorted list of audio file paths relative to the category.
        """
        if subfolder == "/":
            target_dir = os.path.join(self.output_dir, category)
        else:
            target_dir = os.path.join(self.output_dir, category, subfolder)
            
        if not os.path.exists(target_dir):
            return []
            
        files = []
        for f in os.listdir(target_dir):
            full_path = os.path.join(target_dir, f)
            if os.path.isfile(full_path):
                _, ext = os.path.splitext(f)
                if ext.lower() in {".opus", ".mp3", ".wav", ".ogg", ".m4a"}:
                    if subfolder == "/":
                        files.append(f)
                    else:
                        files.append(os.path.join(subfolder, f))
        return sorted(files)

    def setup_components(self):
        """Build the select menus and buttons for the current state.

        Side Effects:
            Mutates the view's UI components.
        """
        self.clear_items()
        
        # 1. Category Select (Row 0)
        category_options = [
            discord.SelectOption(
                label=cat, 
                value=cat, 
                default=(cat == self.selected_category)
            ) for cat in self.categories[:25]
        ]
        category_select = discord.ui.Select(
            placeholder="📁 Selecciona una categoría...", 
            options=category_options, 
            row=0, 
            custom_id="sp_category_select"
        )
        category_select.callback = self.on_category_select
        self.add_item(category_select)
        
        # 2. Subfolder Select (Row 1, if any exist)
        subfolders = self.get_subfolders(self.selected_category)
        has_subfolders = len(subfolders) > 1
        
        row_offset = 0
        if has_subfolders:
            subfolder_options = [
                discord.SelectOption(
                    label="Root (/)" if sub == "/" else sub.replace("/", " ➔ ").replace("\\", " ➔ ").replace("_", " ").replace("-", " ")[:100],
                    value=sub,
                    default=(sub == self.selected_subfolder)
                ) for sub in subfolders[:25]
            ]
            subfolder_select = discord.ui.Select(
                placeholder="📂 Selecciona una subcarpeta...", 
                options=subfolder_options, 
                row=1, 
                custom_id="sp_subfolder_select"
            )
            subfolder_select.callback = self.on_subfolder_select
            self.add_item(subfolder_select)
            row_offset = 1
            
        # 3. Audio Select (Row 1 or Row 2)
        files = self.get_folder_files(self.selected_category, self.selected_subfolder)
        
        page_size = 25
        self.total_pages = max(1, (len(files) + page_size - 1) // page_size)
        if self.current_page >= self.total_pages:
            self.current_page = self.total_pages - 1
        if self.current_page < 0:
            self.current_page = 0
            
        page_files = files[self.current_page * page_size : (self.current_page + 1) * page_size]
        self.files_by_index = {str(i): f for i, f in enumerate(page_files)}
        
        if page_files:
            if not self.selected_file or self.selected_file not in files:
                self.selected_file = page_files[0]
            elif self.selected_file not in page_files:
                self.selected_file = page_files[0]
                
            audio_options = []
            for i, f in enumerate(page_files):
                base_name = os.path.basename(f)
                label = os.path.splitext(base_name)[0].replace("_", " ").replace("-", " ")
                label = label[:100]
                audio_options.append(
                    discord.SelectOption(
                        label=label,
                        value=str(i),
                        default=(f == self.selected_file)
                    )
                )
            audio_select = discord.ui.Select(
                placeholder="🔊 Selecciona un sonido...", 
                options=audio_options, 
                row=row_offset + 1, 
                custom_id="sp_audio_select"
            )
            audio_select.callback = self.on_audio_select
            self.add_item(audio_select)
        else:
            self.selected_file = None
            self.files_by_index = {}
            self.add_item(
                discord.ui.Select(
                    placeholder="⚠️ No hay audios en este directorio", 
                    options=[discord.SelectOption(label="Vacío", value="none")], 
                    disabled=True, 
                    row=row_offset + 1,
                    custom_id="sp_audio_select"
                )
            )
            
        # 4. Action Row (Row 2 or Row 3)
        prev_btn = discord.ui.Button(
            label="◀️", 
            style=discord.ButtonStyle.secondary, 
            row=row_offset + 2, 
            custom_id="btn_sp_prev",
            disabled=(self.current_page == 0)
        )
        prev_btn.callback = self.on_prev_click
        self.add_item(prev_btn)
        
        play_btn = discord.ui.Button(
            label="🔄 Reproducir", 
            style=discord.ButtonStyle.success, 
            row=row_offset + 2,
            custom_id="btn_sp_play"
        )
        play_btn.callback = self.on_play_click
        self.add_item(play_btn)
        
        stop_btn = discord.ui.Button(
            label="⏹️ Detener", 
            style=discord.ButtonStyle.danger, 
            row=row_offset + 2,
            custom_id="btn_sp_stop"
        )
        stop_btn.callback = self.on_stop_click
        self.add_item(stop_btn)
        
        next_btn = discord.ui.Button(
            label="▶️", 
            style=discord.ButtonStyle.secondary, 
            row=row_offset + 2, 
            custom_id="btn_sp_next",
            disabled=(self.current_page == self.total_pages - 1)
        )
        next_btn.callback = self.on_next_click
        self.add_item(next_btn)

    async def update_message(self, interaction: discord.Interaction, status_text: str = None):
        """Render the embed and edit the interaction message.

        Args:
            interaction: Interaction to update.
            status_text: Optional status line shown in the embed.

        Side Effects:
            Edits the original interaction response.

        Async:
            This function is a coroutine and must be awaited.
        """
        embed = discord.Embed(title="🎛️ Soundpad Panel", color=discord.Color.blurple())
        embed.add_field(name="📁 Categoría", value=self.selected_category, inline=True)
        
        sub_label = "Root (/)" if self.selected_subfolder == "/" else self.selected_subfolder.replace("/", " ➔ ").replace("\\", " ➔ ")
        embed.add_field(name="📂 Subcarpeta", value=sub_label, inline=True)
        
        if self.selected_file:
            clean_file = os.path.basename(self.selected_file)
            clean_file = os.path.splitext(clean_file)[0].replace("_", " ").replace("-", " ")
        else:
            clean_file = "Ninguno"
        embed.add_field(name="🔊 Sonido", value=clean_file, inline=True)
        
        embed.set_footer(text=f"Página {self.current_page + 1} de {self.total_pages}")
        
        if status_text: 
            embed.add_field(name="⚡ Estado", value=status_text, inline=False)
            
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            pass

    async def _force_reconnect(self, interaction: discord.Interaction):
        """Reconnect the bot to the user's voice channel if needed.

        Args:
            interaction: Discord interaction with user voice state.

        Returns:
            Tuple of (voice_client, error_message). If error_message is not None
            then reconnection failed.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not interaction.user.voice:
            return None, "No estás en un canal de voz."
        for stale in list(interaction.client.voice_clients):
            if stale.guild.id == interaction.guild.id:
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    pass
        try:
            set_pending_trigger(interaction.user.voice.channel.id, interaction.user.id)
            vc = await interaction.user.voice.channel.connect(reconnect=True, timeout=10.0)
        except Exception as e:
            return None, f"Error al reconectar: {e}"
        return vc, None

    async def play_sound(self, interaction: discord.Interaction):
        """Play the currently selected soundpad audio file.

        Args:
            interaction: Interaction that requested playback.

        Side Effects:
            Connects to voice, plays audio, and emits analytics events.

        Async:
            This function is a coroutine and must be awaited.
        """
        from playCommand import guildPlayers
        if interaction.guild.id in guildPlayers:
            player = guildPlayers[interaction.guild.id]
            if player.currentSong:
                return await interaction.followup.send("⚠️ El bot está reproduciendo música. Por favor, detén la música antes de usar el Soundpad.", ephemeral=True)

        guild = interaction.guild
        vc = guild.voice_client

        if not vc or not vc.is_connected():
            for _ in range(5):
                await asyncio.sleep(0.3)
                vc = guild.voice_client
                if vc and vc.is_connected():
                    break
            if not vc or not vc.is_connected():
                vc, err = await self._force_reconnect(interaction)
                if err:
                    return await interaction.followup.send(f"❌ {err}", ephemeral=True)

        filepath = os.path.join(self.output_dir, self.selected_category, self.selected_file)
        if not os.path.exists(filepath):
            return await interaction.followup.send(f"❌ No encuentro el archivo: {self.selected_file}", ephemeral=True)

        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.2)
        except Exception:
            pass

        try:
            vc.play(discord.FFmpegOpusAudio(filepath))
            analytics.capture("soundpad audio played", user=interaction.user, guild=interaction.guild,
                              properties={"category": self.selected_category,
                                          "audio_file": self.selected_file,
                                          "after_reconnect": False})
            return await self.update_message(interaction, status_text=f"▶️ Reproduciendo: {os.path.basename(self.selected_file)}")
        except discord.ClientException as e:
            print(f"[SOUNDPAD] ClientException on play ({e}); forcing reconnect...")
            analytics.capture("soundpad reconnect attempted", user=interaction.user, guild=interaction.guild,
                              properties={"reason": str(e), "category": self.selected_category,
                                          "audio_file": self.selected_file})
            vc, err = await self._force_reconnect(interaction)
            if err:
                analytics.capture("soundpad playback failed", user=interaction.user, guild=interaction.guild,
                                  properties={"stage": "reconnect", "error": err,
                                              "category": self.selected_category,
                                              "audio_file": self.selected_file})
                return await interaction.followup.send(f"❌ Reconexión falló: {err}", ephemeral=True)
            try:
                vc.play(discord.FFmpegOpusAudio(filepath))
                analytics.capture("soundpad audio played", user=interaction.user, guild=interaction.guild,
                                  properties={"category": self.selected_category,
                                              "audio_file": self.selected_file,
                                              "after_reconnect": True})
                await self.update_message(interaction, status_text=f"▶️ Reproduciendo (tras reconectar): {os.path.basename(self.selected_file)}")
            except Exception as e2:
                print(f"[SOUNDPAD ERROR] Retry failed: {e2}")
                analytics.capture_exception(e2, user=interaction.user, guild=interaction.guild,
                                            properties={"action": "soundpad_retry_play",
                                                        "category": self.selected_category,
                                                        "audio_file": self.selected_file})
                await interaction.followup.send(f"❌ Error tras reconectar: {e2}", ephemeral=True)
        except Exception as e:
            print(f"[SOUNDPAD ERROR] Playback failed: {e}")
            analytics.capture_exception(e, user=interaction.user, guild=interaction.guild,
                                        properties={"action": "soundpad_play",
                                                    "category": self.selected_category,
                                                    "audio_file": self.selected_file})
            await interaction.followup.send(f"❌ Error de reproducción: {e}", ephemeral=True)

    async def on_category_select(self, interaction: discord.Interaction):
        """Handle category selection changes.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Resets pagination state and re-renders the view.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.selected_category = interaction.data["values"][0]
        self.selected_subfolder = "/"
        self.current_page = 0
        self.selected_file = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_subfolder_select(self, interaction: discord.Interaction):
        """Handle subfolder selection changes.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Resets pagination state and re-renders the view.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.selected_subfolder = interaction.data["values"][0]
        self.current_page = 0
        self.selected_file = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_audio_select(self, interaction: discord.Interaction):
        """Handle audio selection changes.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Updates selected file and re-renders the view.

        Async:
            This function is a coroutine and must be awaited.
        """
        selected_index = interaction.data["values"][0]
        self.selected_file = self.files_by_index.get(selected_index)
        self.setup_components()
        await self.update_message(interaction)

    async def on_play_click(self, interaction: discord.Interaction):
        """Handle the Play button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Plays the selected audio file.

        Async:
            This function is a coroutine and must be awaited.
        """
        await interaction.response.defer()
        await self.play_sound(interaction)

    async def on_stop_click(self, interaction: discord.Interaction):
        """Handle the Stop button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Stops current playback and updates the UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if interaction.guild.voice_client: interaction.guild.voice_client.stop()
        analytics.capture("soundpad audio stopped", user=interaction.user, guild=interaction.guild,
                          properties={"category": self.selected_category,
                                      "audio_file": self.selected_file})
        await self.update_message(interaction, status_text="⏹️ Detenido")

    async def on_prev_click(self, interaction: discord.Interaction):
        """Handle the Previous page button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Moves pagination backward and re-renders the UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.current_page > 0:
            self.current_page -= 1
            self.selected_file = None
            self.setup_components()
            await self.update_message(interaction)
        else:
            await interaction.response.defer()

    async def on_next_click(self, interaction: discord.Interaction):
        """Handle the Next page button click.

        Args:
            interaction: Discord interaction payload.

        Side Effects:
            Moves pagination forward and re-renders the UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.selected_file = None
            self.setup_components()
            await self.update_message(interaction)
        else:
            await interaction.response.defer()

async def soundpadLogic(ctx: discord.ApplicationContext):
    """Handle the /soundpad slash command and open the UI panel.

    Args:
        ctx: Discord application context.

    Returns:
        None.

    Side Effects:
        Connects to voice, sends a UI message, and emits analytics events.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not ctx.response.is_done():
        try:
            await ctx.defer()
        except Exception:
            pass

    from playCommand import guildPlayers
    if ctx.guild.id in guildPlayers:
        player = guildPlayers[ctx.guild.id]
        if player.currentSong:
            return await ctx.followup.send("⚠️ El bot está reproduciendo música. Por favor, detén la música antes de usar el Soundpad.", ephemeral=True)

    if not ctx.author.voice:
        return await ctx.followup.send("❌ Debes estar en un canal de voz.", ephemeral=True)

    vc = ctx.guild.voice_client
    if not vc:
        try:
            set_pending_trigger(ctx.author.voice.channel.id, ctx.author.id)
            vc = await ctx.author.voice.channel.connect(reconnect=True, timeout=10.0)
        except Exception as e:
            return await ctx.followup.send(f"❌ Error al conectar: {e}", ephemeral=True)
        # trigger_soundboard_entry ya se dispara por on_voice_state_update
    elif vc.channel.id != ctx.author.voice.channel.id:
        try:
            set_pending_trigger(ctx.author.voice.channel.id, ctx.author.id)
            await vc.move_to(ctx.author.voice.channel)
        except Exception:
            pass

    output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
    try:
        view = SoundpadView(output_dir)
        analytics.capture("soundpad panel opened", user=ctx.author, guild=ctx.guild,
                          properties={"categories_count": len(view.categories),
                                      "default_category": view.selected_category})
        await ctx.followup.send("🎛️ Soundpad Control Panel", view=view)
    except Exception as e:
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "soundpad_panel_open"})
        await ctx.followup.send(f"❌ Error: {e}", ephemeral=True)
