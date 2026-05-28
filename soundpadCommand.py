import os
import asyncio
import discord
import config
import analytics

class SoundpadView(discord.ui.View):
    def __init__(self, output_dir: str):
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
        self.selected_file = None
        self.setup_components()

    def get_category_files(self, category: str):
        cat_dir = os.path.join(self.output_dir, category)
        if not os.path.exists(cat_dir): return []
        files = []
        for f in os.listdir(cat_dir):
            if os.path.isfile(os.path.join(cat_dir, f)):
                _, ext = os.path.splitext(f)
                if ext.lower() in {".opus", ".mp3", ".wav", ".ogg"}:
                    files.append(f)
        return sorted(files)

    def setup_components(self):
        self.clear_items()
        category_options = [discord.SelectOption(label=cat, value=cat, default=(cat == self.selected_category)) for cat in self.categories[:25]]
        category_select = discord.ui.Select(placeholder="📁 Selecciona una categoría...", options=category_options, row=0, custom_id="sp_category_select")
        category_select.callback = self.on_category_select
        self.add_item(category_select)
        
        files = self.get_category_files(self.selected_category)
        if files:
            if not self.selected_file or self.selected_file not in files: self.selected_file = files[0]
            audio_options = [discord.SelectOption(label=os.path.splitext(f)[0][:100], value=f, default=(f == self.selected_file)) for f in files[:25]]
            audio_select = discord.ui.Select(placeholder="🔊 Selecciona un sonido...", options=audio_options, row=1, custom_id="sp_audio_select")
            audio_select.callback = self.on_audio_select
            self.add_item(audio_select)
        else:
            self.selected_file = None
            self.add_item(discord.ui.Select(placeholder="⚠️ No hay audios", options=[discord.SelectOption(label="Vacío", value="none")], disabled=True, row=1))
            
        play_btn = discord.ui.Button(label="🔄 Reproducir", style=discord.ButtonStyle.success, row=2)
        play_btn.callback = self.on_play_click
        self.add_item(play_btn)
        
        stop_btn = discord.ui.Button(label="⏹️ Detener", style=discord.ButtonStyle.danger, row=2)
        stop_btn.callback = self.on_stop_click
        self.add_item(stop_btn)

    async def update_message(self, interaction: discord.Interaction, status_text: str = None):
        embed = discord.Embed(title="🎛️ Soundpad Panel", color=discord.Color.blurple())
        embed.add_field(name="📁 Categoría", value=self.selected_category, inline=True)
        clean_file = os.path.splitext(self.selected_file)[0] if self.selected_file else "Ninguno"
        embed.add_field(name="🔊 Sonido", value=clean_file, inline=True)
        if status_text: embed.add_field(name="⚡ Estado", value=status_text, inline=False)
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            # Original message was deleted; nothing to do.
            pass

    async def _force_reconnect(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            return None, "No estás en un canal de voz."
        for stale in list(interaction.client.voice_clients):
            if stale.guild.id == interaction.guild.id:
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    pass
        try:
            vc = await interaction.user.voice.channel.connect(reconnect=True, timeout=10.0)
        except Exception as e:
            return None, f"Error al reconectar: {e}"
        try:
            from bot import start_listening
            await start_listening(vc)
        except Exception:
            pass
        return vc, None

    async def play_sound(self, interaction: discord.Interaction):
        guild = interaction.guild
        vc = guild.voice_client

        # _connected puede estar False transitoriamente durante renegociación DAVE.
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
            return await self.update_message(interaction, status_text=f"▶️ Reproduciendo: {self.selected_file}")
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
                await self.update_message(interaction, status_text=f"▶️ Reproduciendo (tras reconectar): {self.selected_file}")
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
        self.selected_category = interaction.data["values"][0]
        self.selected_file = None
        self.setup_components()
        await self.update_message(interaction)

    async def on_audio_select(self, interaction: discord.Interaction):
        self.selected_file = interaction.data["values"][0]
        self.setup_components()
        await interaction.response.defer()
        await self.play_sound(interaction)

    async def on_play_click(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.play_sound(interaction)

    async def on_stop_click(self, interaction: discord.Interaction):
        if interaction.guild.voice_client: interaction.guild.voice_client.stop()
        analytics.capture("soundpad audio stopped", user=interaction.user, guild=interaction.guild,
                          properties={"category": self.selected_category,
                                      "audio_file": self.selected_file})
        await self.update_message(interaction, status_text="⏹️ Detenido")

async def soundpadLogic(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        return await ctx.respond("❌ Debes estar en un canal de voz.")
    
    vc = ctx.guild.voice_client
    if not vc:
        try:
            vc = await ctx.author.voice.channel.connect(reconnect=True, timeout=10.0)
        except Exception as e:
            return await ctx.respond(f"❌ Error al conectar: {e}")
        from bot import start_listening
        await start_listening(vc)
        # trigger_soundboard_entry ya se dispara por on_voice_state_update
    elif vc.channel.id != ctx.author.voice.channel.id:
        await vc.move_to(ctx.author.voice.channel)

    output_dir = getattr(config, "CUSTOM_AUDIO_PATH", "audio_output")
    try:
        view = SoundpadView(output_dir)
        analytics.capture("soundpad panel opened", user=ctx.author, guild=ctx.guild,
                          properties={"categories_count": len(view.categories),
                                      "default_category": view.selected_category})
        await ctx.respond("🎛️ Soundpad Control Panel", view=view)
    except Exception as e:
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "soundpad_panel_open"})
        await ctx.respond(f"❌ Error: {e}")
