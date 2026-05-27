import os
import asyncio
import discord
import config

# Global dictionary to track active player states per guild
guildPlayers = {}

class GuildPlayer:
    def __init__(self, guildId: int, bot):
        self.guildId = guildId
        self.bot = bot
        self.queue = []         # List of {"id": str, "title": str}
        self.history = []       # List of {"id": str, "title": str}
        self.currentSong = None # {"id": str, "title": str} or None
        self.vc = None
        self.controlMessage = None
        self.textChannel = None
        self.isStopping = False
        self.isPrevious = False

    async def addSongs(self, songs, ctx):
        self.textChannel = ctx.channel
        self.queue.extend(songs)

        from bot import safeEdit
        if len(songs) > 1:
            await safeEdit(ctx, f"✅ Se añadieron **{len(songs)}** canciones a la cola.")
        else:
            await safeEdit(ctx, f"✅ Se añadió **{songs[0]['title']}** a la cola.")

        # If not playing anything, start playing the first song in queue
        if not self.currentSong:
            if self.queue:
                self.currentSong = self.queue.pop(0)
                await self.startPlayingCurrent()
        else:
            await self.updateControlMessage()

    async def startPlayingCurrent(self):
        if not self.currentSong or not self.vc:
            return

        videoId = self.currentSong["id"]
        videoTitle = self.currentSong["title"]

        downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(downloadsDir, exist_ok=True)
        filepath = os.path.join(downloadsDir, f"{videoId}.mp3")

        await self.updateControlMessage(f"⬇️ Descargando e introduciendo al buffer: **{videoTitle}**...")

        # Download song if not already cached
        if not os.path.exists(filepath):
            try:
                ytDlpPath = config.YT_DLP_PATH
                inputStr = f"https://www.youtube.com/watch?v={videoId}"
                proc = await asyncio.create_subprocess_exec(
                    ytDlpPath,
                    
                    "-x",
                    "--audio-format", "mp3",
                    "--no-playlist",
                    "-o", os.path.join(downloadsDir, "%(id)s.%(ext)s"),
                    inputStr,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if proc.returncode != 0:
                    await self.updateControlMessage(f"❌ Error al descargar {videoTitle}.")
                    # Skip to next song
                    self.bot.loop.create_task(self.skipSong())
                    return
            except Exception as e:
                print(f"[PLAYER ERROR] Download exception: {e}")
                await self.updateControlMessage(f"❌ Error al descargar {videoTitle}.")
                self.bot.loop.create_task(self.skipSong())
                return

        # Start playback
        try:
            audioSource = discord.FFmpegOpusAudio(filepath)

            def afterCallback(error):
                asyncio.run_coroutine_threadsafe(self.onSongFinished(error), self.bot.loop)

            self.vc.play(audioSource, after=afterCallback)
            await self.updateControlMessage()
        except Exception as e:
            print(f"[PLAYER ERROR] Playback start exception: {e}")
            await self.updateControlMessage(f"❌ Error al reproducir {videoTitle}: {e}")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            self.bot.loop.create_task(self.skipSong())

    async def onSongFinished(self, error):
        if error:
            print(f"[PLAYER] Playback error: {error}")

        # Delete the file of the finished song
        if self.currentSong:
            videoId = self.currentSong["id"]
            downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    print(f"[PLAYER] Deleted temporary file: {filepath}")
            except Exception as e:
                print(f"[PLAYER] Error deleting file {filepath}: {e}")

        # Stop action
        if self.isStopping:
            self.isStopping = False
            self.currentSong = None
            await self.updateControlMessage("⏹️ Reproducción detenida y cola vaciada.")
            return

        # Previous action
        if self.isPrevious:
            self.isPrevious = False
            if self.history:
                if self.currentSong:
                    self.queue.insert(0, self.currentSong)
                self.currentSong = self.history.pop()
                await self.startPlayingCurrent()
            else:
                self.currentSong = None
                await self.updateControlMessage("⚠️ No hay canciones anteriores.")
            return

        # Skip or natural finish: add current song to history
        if self.currentSong:
            self.history.append(self.currentSong)

        # Play next in queue
        if self.queue:
            self.currentSong = self.queue.pop(0)
            await self.startPlayingCurrent()
        else:
            self.currentSong = None
            await self.updateControlMessage("⏹️ Fin de la cola de reproducción.")

    async def togglePausePlay(self):
        if self.vc:
            if self.vc.is_playing():
                self.vc.pause()
                await self.updateControlMessage()
            elif self.vc.is_paused():
                self.vc.resume()
                await self.updateControlMessage()

    async def skipSong(self):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            await self.onSongFinished(None)

    async def playPrevious(self):
        if not self.history:
            return
        self.isPrevious = True
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            await self.onSongFinished(None)

    async def stopPlayback(self):
        self.isStopping = True
        self.queue.clear()
        if self.vc:
            if self.vc.is_playing() or self.vc.is_paused():
                self.vc.stop()
            else:
                await self.onSongFinished(None)

    async def updateControlMessage(self, customStatus=None):
        if not self.textChannel:
            return

        embed = discord.Embed(title="🎵 Reproductor de Música", color=discord.Color.blurple())

        # Determine status text
        if customStatus:
            status = customStatus
        elif self.vc and self.vc.is_paused():
            status = f"⏸️ Pausado: **{self.currentSong['title']}**"
        elif self.vc and self.vc.is_playing():
            status = f"▶️ Reproduciendo: **{self.currentSong['title']}**"
        else:
            status = "⏹️ Sin reproducción activa."

        embed.description = status

        # Queue list
        if self.queue:
            queueLines = []
            for i, song in enumerate(self.queue[:5]):
                queueLines.append(f"{i+1}. {song['title']}")
            if len(self.queue) > 5:
                queueLines.append(f"... y {len(self.queue) - 5} más.")
            embed.add_field(name="📋 Siguientes en cola", value="\n".join(queueLines), inline=False)
        else:
            embed.add_field(name="📋 Siguientes en cola", value="La cola está vacía.", inline=False)

        # History footer
        if self.history:
            embed.set_footer(text=f"Canciones en historial: {len(self.history)}")

        view = PlayerControlView(self)

        try:
            if self.controlMessage:
                await self.controlMessage.edit(embed=embed, view=view)
            else:
                self.controlMessage = await self.textChannel.send(embed=embed, view=view)
        except Exception as e:
            print(f"[PLAYER] Error updating control message: {e}")
            try:
                self.controlMessage = await self.textChannel.send(embed=embed, view=view)
            except Exception:
                pass


class PlayerControlView(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.updateButtonStates()

    def updateButtonStates(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "btn_prev":
                    child.disabled = len(self.player.history) == 0
                elif child.custom_id == "btn_pause_play":
                    if self.player.vc and self.player.vc.is_paused():
                        child.label = "▶️ Reanudar"
                        child.style = discord.ButtonStyle.success
                    else:
                        child.label = "⏸️ Pausar"
                        child.style = discord.ButtonStyle.primary
                elif child.custom_id == "btn_next":
                    child.disabled = len(self.player.queue) == 0

    @discord.ui.button(label="⏮️ Anterior", style=discord.ButtonStyle.secondary, custom_id="btn_prev")
    async def previousButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.playPrevious()

    @discord.ui.button(label="⏸️ Pausar", style=discord.ButtonStyle.primary, custom_id="btn_pause_play")
    async def pausePlayButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.togglePausePlay()

    @discord.ui.button(label="⏭️ Siguiente", style=discord.ButtonStyle.secondary, custom_id="btn_next")
    async def nextButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.skipSong()

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def stopButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.stopPlayback()


def getGuildPlayer(guildId: int, bot) -> GuildPlayer:
    if guildId not in guildPlayers:
        guildPlayers[guildId] = GuildPlayer(guildId, bot)
    return guildPlayers[guildId]

def clearGuildPlayer(guildId: int):
    if guildId in guildPlayers:
        player = guildPlayers[guildId]
        player.queue.clear()
        player.history.clear()
        player.currentSong = None
        player.vc = None
        player.controlMessage = None
        player.textChannel = None
        del guildPlayers[guildId]

async def playLogic(ctx: discord.ApplicationContext, query: str):
    from bot import safe_defer, safe_respond, safeEdit

    if not await safe_defer(ctx):
        return

    # Ensure user is in a voice channel
    if not ctx.author.voice:
        return await safe_respond(ctx, "❌ ¡Debes estar en un canal de voz!")

    channel = ctx.author.voice.channel

    # Connect or move bot to the channel
    if ctx.voice_client is None:
        try:
            vc = await channel.connect(reconnect=True)
        except Exception as e:
            return await safe_respond(ctx, f"❌ Error al conectar al canal: {e}")
    else:
        vc = ctx.voice_client
        if vc.channel.id != channel.id:
            # Stop recording if active
            if getattr(vc, "recording", False):
                try:
                    vc.stop_recording()
                except Exception:
                    pass
                setattr(vc, "recording", False)
            await vc.move_to(channel)

    player = getGuildPlayer(ctx.guild.id, ctx.bot)
    player.vc = vc
    player.textChannel = ctx.channel

    # Prepare search or URL input
    inputStr = query.strip()
    if not (inputStr.startswith("http://") or inputStr.startswith("https://") or inputStr.startswith("ytsearch:")):
        inputStr = f"ytsearch1:{inputStr}"

    # 1. Fetch metadata (ID and Title)
    await safeEdit(ctx, "🔍 Buscando y obteniendo metadatos...")
    try:
        proc = await asyncio.create_subprocess_exec(
            config.YT_DLP_PATH,
            
            "--flat-playlist",
            "--simulate",
            "--print", "%(id)s",
            "--print", "%(title)s",
            inputStr,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            errMsg = stderr.decode('utf-8', errors='replace').strip()
            print(f"[PLAY ERROR] Metadata fetch failed: {errMsg}")
            return await safeEdit(ctx, f"❌ Error al buscar el video: {errMsg[:200]}")

        lines = stdout.decode('utf-8', errors='replace').strip().split('\n')
        lines = [l.strip() for l in lines if l.strip()]
        if not lines:
            return await safeEdit(ctx, "❌ No se encontraron resultados.")

        songs = []
        for i in range(0, len(lines) - 1, 2):
            songs.append({"id": lines[i], "title": lines[i+1]})

        if not songs:
            return await safeEdit(ctx, "❌ No se pudieron obtener los metadatos del video.")
    except Exception as e:
        print(f"[PLAY ERROR] Exception during metadata fetch: {e}")
        return await safeEdit(ctx, f"❌ Error al buscar el video: {e}")

    # Add songs to player queue
    await player.addSongs(songs, ctx)
