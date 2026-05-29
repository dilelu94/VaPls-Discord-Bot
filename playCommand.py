import os
import asyncio
import discord
import config
import analytics
import time
import logging
from logging.handlers import RotatingFileHandler
from greeting import set_pending_trigger

# Configure a rotating logger for play command steps
playLogger = logging.getLogger("play_logger")
playLogger.setLevel(logging.INFO)
playLogPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "play.log")
handler = RotatingFileHandler(playLogPath, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)
playLogger.addHandler(handler)
playLogger.propagate = True

# Clean up downloads directory on load to remove stale files
_downloadsDirInit = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
if os.path.exists(_downloadsDirInit):
    for _f in os.listdir(_downloadsDirInit):
        if _f.endswith(".mp3") or _f.endswith(".webm"):
            try:
                os.remove(os.path.join(_downloadsDirInit, _f))
            except Exception:
                pass

# Global dictionary to track active player states per guild
guildPlayers = {}

class CancelDownloadView(discord.ui.View):
    def __init__(self, player, videoId: str, videoTitle: str):
        super().__init__(timeout=60)
        self.player = player
        self.videoId = videoId
        self.videoTitle = videoTitle

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, custom_id="btn_cancel_dl")
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.cancelDownload(self.videoId, self.videoTitle, interaction)

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
        self.lastRequester = None  # discord.Member of the user who last queued songs
        self.isDownloading = False
        self.downloadingIds = set()
        self.preDownloadTask = None
        self.activeDownloadProc = None
        self.initialCtx = None

    async def addSongs(self, songs, ctx):
        self.textChannel = ctx.channel
        self.lastRequester = ctx.author
        
        isFirst = (not self.currentSong and len(self.queue) == 0)
        self.queue.extend(songs)

        analytics.capture("play songs queued", user=ctx.author, guild=ctx.guild,
                          properties={"count": len(songs),
                                      "queue_length": len(self.queue),
                                      "first_title": songs[0]["title"] if songs else None})

        from bot import safeEdit
        estimatedTime = int(time.time() + 30)
        if len(songs) > 1:
            if isFirst:
                view = CancelDownloadView(self, songs[0]["id"], f"Playlist ({len(songs)} canciones)")
                await ctx.interaction.edit_original_response(content=f"✅ Descargando playlist (se añadieron **{len(songs)}** canciones, iniciando con **{songs[0]['title']}**... <t:{estimatedTime}:R>)", view=view)
            else:
                await safeEdit(ctx, f"✅ Se añadieron **{len(songs)}** canciones a la cola.")
        else:
            if isFirst:
                view = CancelDownloadView(self, songs[0]["id"], songs[0]["title"])
                await ctx.interaction.edit_original_response(content=f"✅ Descargando: **{songs[0]['title']}**... <t:{estimatedTime}:R>", view=view)
            else:
                await safeEdit(ctx, f"✅ Se añadió **{songs[0]['title']}** a la cola.")

        # If not playing anything, start playing the first song in queue
        if not self.currentSong:
            if self.queue:
                self.currentSong = self.queue.pop(0)
                self.initialCtx = ctx
                await self.startPlayingCurrent()
        else:
            await self.updateControlMessage()
            self.startPreDownloading()

    async def cancelDownload(self, videoId: str, videoTitle: str, interaction: discord.Interaction):
        playLogger.info(f"[DOWNLOAD CANCEL] User cancelled download for '{videoTitle}' (ID: {videoId})")
        if self.activeDownloadProc:
            try:
                self.activeDownloadProc.kill()
            except Exception:
                pass
        self.queue.clear()
        self.currentSong = None
        self.isDownloading = False
        self.downloadingIds.discard(videoId)
        self.initialCtx = None
        try:
            await interaction.edit_original_response(content=f"❌ Descarga cancelada: **{videoTitle}**.", view=None)
        except Exception:
            pass
        if self.vc:
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
            self.vc = None

    async def startPlayingCurrent(self):
        if not self.currentSong or not self.vc:
            return

        videoId = self.currentSong["id"]
        videoTitle = self.currentSong["title"]

        downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(downloadsDir, exist_ok=True)
        filepath = os.path.join(downloadsDir, f"{videoId}.mp3")

        guild = getattr(self.vc, "guild", None)

        # Wait if currently downloading in background
        if videoId in self.downloadingIds:
            playLogger.info(f"[PLAYBACK WAIT] Song '{videoTitle}' (ID: {videoId}) is downloading in background. Waiting...")
            while videoId in self.downloadingIds:
                await asyncio.sleep(0.5)

        # Download song if not already cached
        if not os.path.exists(filepath):
            self.isDownloading = True
            self.downloadingIds.add(videoId)
            if self.controlMessage is not None:
                await self.updateControlMessage()
            playLogger.info(f"[DOWNLOAD START] Downloading '{videoTitle}' (ID: {videoId})...")
            startTime = time.time()
            try:
                ytDlpPath = config.YT_DLP_PATH
                inputStr = f"https://www.youtube.com/watch?v={videoId}"
                cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
                ytDlpArgs = [ytDlpPath]
                if os.path.exists(cookiesPath):
                    ytDlpArgs += ["--cookies", cookiesPath]
                ytDlpArgs += [
                    "-x",
                    "--audio-format", "mp3",
                    "--no-playlist",
                    "-o", os.path.join(downloadsDir, "%(id)s.%(ext)s"),
                    inputStr,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *ytDlpArgs,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                self.activeDownloadProc = proc
                await proc.communicate()
                self.activeDownloadProc = None

                if not self.currentSong:
                    # Download was cancelled
                    return

                if proc.returncode != 0:
                    self.isDownloading = False
                    analytics.capture("play song failed", user=self.lastRequester, guild=guild,
                                      properties={"stage": "download", "video_id": videoId,
                                                  "title": videoTitle, "returncode": proc.returncode})
                    playLogger.error(f"[DOWNLOAD FAIL] Failed to download '{videoTitle}' (ID: {videoId}) with returncode {proc.returncode}")
                    if self.initialCtx:
                        try:
                            await self.initialCtx.interaction.edit_original_response(
                                content=f"❌ Error al descargar: **{videoTitle}**",
                                view=None
                            )
                        except Exception:
                            pass
                        self.initialCtx = None
                    if self.controlMessage is not None:
                        await self.updateControlMessage(f"❌ Error al descargar {videoTitle}.")
                    # Skip to next song
                    self.bot.loop.create_task(self.skipSong())
                    return
                else:
                    duration = time.time() - startTime
                    fileSize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    playLogger.info(f"[DOWNLOAD SUCCESS] Successfully downloaded '{videoTitle}' (ID: {videoId}) in {duration:.2f}s. File size: {fileSize} bytes ({fileSize / (1024*1024):.2f} MB)")
            except Exception as e:
                self.activeDownloadProc = None
                if not self.currentSong:
                    # Download was cancelled
                    return
                self.isDownloading = False
                print(f"[PLAYER ERROR] Download exception: {e}")
                analytics.capture_exception(e, user=self.lastRequester, guild=guild,
                                            properties={"stage": "download", "video_id": videoId,
                                                        "title": videoTitle})
                playLogger.error(f"[DOWNLOAD ERROR] Exception downloading '{videoTitle}': {e}")
                if self.initialCtx:
                    try:
                        await self.initialCtx.interaction.edit_original_response(
                            content=f"❌ Error al descargar: **{videoTitle}**",
                            view=None
                        )
                    except Exception:
                        pass
                    self.initialCtx = None
                if self.controlMessage is not None:
                    await self.updateControlMessage(f"❌ Error al descargar {videoTitle}.")
                self.bot.loop.create_task(self.skipSong())
                return
            finally:
                self.isDownloading = False
                self.downloadingIds.discard(videoId)
                self.activeDownloadProc = None

        # Start playback
        try:
            # Delete/cleanup the initial downloading message
            if self.initialCtx:
                try:
                    await self.initialCtx.interaction.delete_original_response()
                except Exception as e:
                    playLogger.warning(f"[PLAYBACK START] Could not delete original response: {e}")
                self.initialCtx = None

            audioSource = discord.FFmpegOpusAudio(filepath)

            def afterCallback(error):
                asyncio.run_coroutine_threadsafe(self.onSongFinished(error), self.bot.loop)

            self.vc.play(audioSource, after=afterCallback)
            analytics.capture("play song started", user=self.lastRequester, guild=guild,
                               properties={"video_id": videoId, "title": videoTitle,
                                           "queue_length": len(self.queue)})
            await self.updateControlMessage()
            playLogger.info(f"[PLAYBACK START] Started playing '{videoTitle}' (ID: {videoId})")
            
            # Start background pre-downloading of the queue
            self.startPreDownloading()
        except Exception as e:
            print(f"[PLAYER ERROR] Playback start exception: {e}")
            analytics.capture_exception(e, user=self.lastRequester, guild=guild,
                                         properties={"stage": "play", "video_id": videoId, "title": videoTitle})
            playLogger.error(f"[PLAYBACK ERROR] Playback start exception for '{videoTitle}': {e}")
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
            playLogger.error(f"[PLAYBACK ERROR] Playback finished with error for '{self.currentSong['title'] if self.currentSong else 'Unknown'}': {error}")

        # Delete the file of the finished song
        if self.currentSong:
            videoId = self.currentSong["id"]
            downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            try:
                if os.path.exists(filepath):
                    fileSize = os.path.getsize(filepath)
                    os.remove(filepath)
                    playLogger.info(f"[CLEANUP] Deleted temporary file for '{self.currentSong['title']}' (ID: {videoId}). Size: {fileSize} bytes")
            except Exception as e:
                print(f"[PLAYER] Error deleting file {filepath}: {e}")
                playLogger.error(f"[CLEANUP ERROR] Error deleting file {filepath}: {e}")

        # Stop action
        if self.isStopping:
            self.isStopping = False
            title = self.currentSong["title"] if self.currentSong else "Unknown"
            playLogger.info(f"[PLAYBACK STOP] Playback stopped. Queue cleared.")
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
                playLogger.info(f"[PLAYBACK PREVIOUS] Loading previous song: '{self.currentSong['title']}'")
                await self.startPlayingCurrent()
            else:
                self.currentSong = None
                await self.updateControlMessage("⚠️ No hay canciones anteriores.")
            return

        # Skip or natural finish: add current song to history
        if self.currentSong:
            playLogger.info(f"[PLAYBACK FINISH] Finished playing '{self.currentSong['title']}' (ID: {self.currentSong['id']})")
            self.history.append(self.currentSong)

        # Play next in queue
        if self.queue:
            self.currentSong = self.queue.pop(0)
            await self.startPlayingCurrent()
        else:
            self.currentSong = None
            await self.updateControlMessage("⏹️ Fin de la cola de reproducción.")

    async def predownloadQueue(self):
        # We run a loop as long as the player is active and there are items in the queue
        while self.vc and (self.vc.is_playing() or self.vc.is_paused()) and self.queue:
            # Find the first item in the queue that is not yet downloaded and not currently downloading
            targetSong = None
            for song in self.queue:
                vid = song["id"]
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads", f"{vid}.mp3")
                if not os.path.exists(path) and vid not in self.downloadingIds:
                    targetSong = song
                    break
            
            if not targetSong:
                # All songs in queue are either downloaded or currently downloading
                break
                
            videoId = targetSong["id"]
            videoTitle = targetSong["title"]
            downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            
            self.downloadingIds.add(videoId)
            playLogger.info(f"[PRE-DOWNLOAD START] Background downloading queue item '{videoTitle}' (ID: {videoId})...")
            startTime = time.time()
            try:
                ytDlpPath = config.YT_DLP_PATH
                inputStr = f"https://www.youtube.com/watch?v={videoId}"
                cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
                ytDlpArgs = [ytDlpPath]
                if os.path.exists(cookiesPath):
                    ytDlpArgs += ["--cookies", cookiesPath]
                ytDlpArgs += [
                    "-x",
                    "--audio-format", "mp3",
                    "--no-playlist",
                    "-o", os.path.join(downloadsDir, "%(id)s.%(ext)s"),
                    inputStr,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *ytDlpArgs,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if proc.returncode == 0:
                    duration = time.time() - startTime
                    fileSize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    playLogger.info(f"[PRE-DOWNLOAD SUCCESS] Background downloaded '{videoTitle}' (ID: {videoId}) in {duration:.2f}s. Size: {fileSize} bytes ({fileSize / (1024*1024):.2f} MB)")
                else:
                    playLogger.warning(f"[PRE-DOWNLOAD FAIL] Failed to background download '{videoTitle}' (ID: {videoId}) with code {proc.returncode}")
            except Exception as e:
                playLogger.error(f"[PRE-DOWNLOAD ERROR] Exception background downloading '{videoTitle}': {e}")
            finally:
                self.downloadingIds.discard(videoId)
                
            # Sleep slightly between downloads to avoid high CPU load/network spam
            await asyncio.sleep(1)

    def startPreDownloading(self):
        if not self.preDownloadTask or self.preDownloadTask.done():
            self.preDownloadTask = self.bot.loop.create_task(self.predownloadQueue())

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
        self.isDownloading = False
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
        elif getattr(self, "isDownloading", False) and self.currentSong:
            status = f"⬇️ Descargando: **{self.currentSong['title']}**"
        elif self.vc and self.vc.is_paused():
            durStr = self.currentSong.get("duration_string", "")
            durSuffix = f" `[{durStr}]`" if durStr else ""
            status = f"⏸️ Pausado: **{self.currentSong['title']}**{durSuffix}"
        elif self.vc and self.vc.is_playing():
            durStr = self.currentSong.get("duration_string", "")
            durSuffix = f" `[{durStr}]`" if durStr else ""
            status = f"▶️ Reproduciendo: **{self.currentSong['title']}**{durSuffix}"
        else:
            status = "⏹️ Sin reproducción activa."

        embed.description = status

        # Queue list
        if self.queue:
            queueLines = []
            for i, song in enumerate(self.queue[:5]):
                durStr = song.get("duration_string", "")
                durSuffix = f" `[{durStr}]`" if durStr else ""
                queueLines.append(f"{i+1}. {song['title']}{durSuffix}")
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
        # Cancel background downloader task
        if getattr(player, "preDownloadTask", None) and not player.preDownloadTask.done():
            player.preDownloadTask.cancel()
            playLogger.info(f"[CLEANUP] Cancelled active background pre-download task for guild {guildId}")

        # Delete all files in queue and currentSong
        downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        filesToDelete = []
        if player.currentSong:
            filesToDelete.append(player.currentSong["id"])
        for song in player.queue:
            filesToDelete.append(song["id"])
            
        for videoId in filesToDelete:
            filepath = os.path.join(downloadsDir, f"{videoId}.mp3")
            webmpath = os.path.join(downloadsDir, f"{videoId}.webm")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    playLogger.info(f"[CLEANUP] Cleaned up file {filepath}")
            except Exception:
                pass
            try:
                if os.path.exists(webmpath):
                    os.remove(webmpath)
            except Exception:
                pass

        player.queue.clear()
        player.history.clear()
        player.currentSong = None
        player.vc = None
        player.controlMessage = None
        player.textChannel = None
        player.isDownloading = False
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
            set_pending_trigger(channel.id, ctx.author.id)
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
            set_pending_trigger(channel.id, ctx.author.id)
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
        cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
        ytDlpArgs = [config.YT_DLP_PATH]
        if os.path.exists(cookiesPath):
            ytDlpArgs += ["--cookies", cookiesPath]
        ytDlpArgs += [
            "--flat-playlist",
            "--simulate",
            "--print", "%(id)s",
            "--print", "%(title)s",
            "--print", "%(duration_string)s",
            inputStr,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ytDlpArgs,
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
        for i in range(0, len(lines) - 2, 3):
            durStr = lines[i+2]
            songs.append({
                "id": lines[i],
                "title": lines[i+1],
                "duration_string": durStr if durStr != "NA" else ""
            })

        if not songs:
            return await safeEdit(ctx, "❌ No se pudieron obtener los metadatos del video.")
    except Exception as e:
        print(f"[PLAY ERROR] Exception during metadata fetch: {e}")
        return await safeEdit(ctx, f"❌ Error al buscar el video: {e}")

    # Add songs to player queue
    await player.addSongs(songs, ctx)
