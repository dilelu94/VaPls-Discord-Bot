"""Music playback and queue management for the /play command.

Defines the GuildPlayer lifecycle, handles yt-dlp downloads, FFmpeg playback,
and interactive UI controls for queue management. Depends on py-cord, yt-dlp,
FFmpeg, config, analytics, and greeting triggers.
"""
import os
import asyncio
import discord
import config
import analytics
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

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

# How many search candidates to offer when /play (or the indio) finds several
# matches for a free-text query. Keeps the "¿cuál querés?" list readable.
_PLAY_CHOICE_COUNT = 5


def _diagnoseYtDlpFailure(stderr: str, returncode: int = 0) -> str:
    """Mapea stderr de yt-dlp (o exception) a un mensaje accionable para el usuario.

    Devuelve un string corto que explica la causa probable. Si no matchea ningún
    patrón conocido, devuelve la última línea no vacía del stderr (recortada).
    """
    if not stderr:
        if returncode == 2 or "No such file" in str(returncode):
            return "yt-dlp no está instalado en el server."
        return f"yt-dlp falló (returncode={returncode}) sin output. Revisá play.log."

    s = stderr.lower()
    # Discord/UX-friendly diagnostics ordered by specificity.
    if "sign in to confirm you're not a bot" in s or "confirm you're not a bot" in s:
        return ("YouTube pide login (bot-check). Las cookies del server pueden estar "
                "caducas — re-exportalas y subílas con upload-cookies-discord-bot.sh.")
    if "sign in to confirm your age" in s or "age-restricted" in s or "age restricted" in s:
        return "El video tiene restricción de edad y las cookies del server no la pasan."
    if "members-only" in s or "members only" in s or "join this channel to get access" in s:
        return "El video es members-only del canal — no se puede descargar."
    if "private video" in s or "this video is private" in s:
        return "El video es privado."
    if "video unavailable" in s or "this video is unavailable" in s:
        return "Video no disponible (eliminado o bloqueado en tu región)."
    if "premiere will begin" in s or "premieres in" in s:
        return "Es un premiere que todavía no empezó."
    if "live event will begin" in s or "this live event" in s:
        return "Es un live que todavía no empezó."
    if "video is no longer available" in s or "copyright" in s:
        return "Video bloqueado por copyright."
    if "http error 429" in s or "too many requests" in s:
        return "YouTube nos rate-limiteó (HTTP 429). Esperá unos minutos."
    if "http error 403" in s:
        return "YouTube devolvió 403 — probable bot-check o token de stream vencido."
    if "unable to extract" in s and "player response" in s:
        return ("yt-dlp no pudo parsear el video — probablemente está desactualizado. "
                "Correr: pip install --user --upgrade --pre yt-dlp")
    if "no supported javascript runtime" in s:
        return "Falta deno en el server (yt-dlp lo necesita para resolver JS de YouTube)."
    if "no video formats found" in s or "requested format is not available" in s:
        return "No hay formato de audio disponible para ese video."
    if "name or service not known" in s or "temporary failure in name resolution" in s:
        return "El server no resuelve DNS (problema de red)."
    if "connection refused" in s or "connection reset" in s:
        return "Conexión rechazada/reseteada (red o YouTube cayéndose)."
    # Fallback: última línea no vacía de stderr, recortada.
    last = next((ln.strip() for ln in reversed(stderr.splitlines()) if ln.strip()), "")
    return last[:300] if last else f"yt-dlp falló (returncode={returncode})."

class CancelDownloadView(discord.ui.View):
    """UI view that lets a user cancel the initial yt-dlp download."""
    def __init__(self, player, videoId: str, videoTitle: str):
        """Initialize the cancel button view.

        Args:
            player: GuildPlayer instance that owns the download.
            videoId: YouTube video ID currently downloading.
            videoTitle: Display title for the cancel confirmation.
        """
        super().__init__(timeout=60)
        self.player = player
        self.videoId = videoId
        self.videoTitle = videoTitle

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, custom_id="btn_cancel_dl")
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Cancel button click.

        Args:
            button: The Discord UI button instance.
            interaction: Interaction that triggered the click.

        Side Effects:
            Aborts the download, clears the queue, and disconnects voice.

        Async:
            This function is a coroutine and must be awaited.
        """
        await interaction.response.defer()
        await self.player.cancelDownload(self.videoId, self.videoTitle, interaction)

class GuildPlayer:
    """Per-guild playback state and queue manager."""
    def __init__(self, guildId: int, bot):
        """Initialize the player state for a guild.

        Args:
            guildId: Discord guild ID.
            bot: Discord bot client.
        """
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

    async def _enqueueAndMaybeStart(self, songs, *, source: Optional[str] = None):
        """Shared enqueue → analytics → maybe-start-playback core.

        Used by both the /play slash entrypoint (via ``addSongs``) and
        programmatic entrypoints (``playFromIndio``). The caller is
        responsible for posting any user-facing status message; this method
        only manages internal player state.

        Args:
            songs: List of dicts with id/title metadata.
            source: Optional tag for analytics (e.g. ``"indio"``).

        Side Effects:
            Mutates queue/currentSong, fires analytics, kicks off playback
            or pre-download.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.queue.extend(songs)

        guild = self.bot.get_guild(self.guildId) if self.bot else None
        props = {
            "count": len(songs),
            "queue_length": len(self.queue),
            "first_title": songs[0]["title"] if songs else None,
        }
        if source:
            props["source"] = source
        analytics.capture("play songs queued", user=self.lastRequester,
                          guild=guild, properties=props)

        if not self.currentSong and self.queue:
            self.currentSong = self.queue.pop(0)
            await self.startPlayingCurrent()
        else:
            await self.updateControlMessage()
            self.startPreDownloading()

    async def addSongs(self, songs, ctx):
        """Add one or more songs to the queue and start playback if idle.

        Args:
            songs: List of dicts with id/title metadata.
            ctx: Discord application context.

        Returns:
            None.

        Side Effects:
            Updates queue, sends Discord messages, and may start playback.

        Async:
            This function is a coroutine and must be awaited.
        """
        from bot import safeEdit

        self.textChannel = ctx.channel
        self.lastRequester = ctx.author

        isFirst = (not self.currentSong and len(self.queue) == 0)
        if isFirst:
            # startPlayingCurrent will delete this interaction's original
            # response once playback actually starts.
            self.initialCtx = ctx

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

        await self._enqueueAndMaybeStart(songs)

    async def cancelDownload(self, videoId: str, videoTitle: str, interaction: discord.Interaction):
        """Cancel an active download and reset playback state.

        Args:
            videoId: Video ID to cancel.
            videoTitle: Display name for notifications.
            interaction: Interaction used to edit the response.

        Side Effects:
            Kills download subprocess, clears queue, and disconnects voice.

        Async:
            This function is a coroutine and must be awaited.
        """
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
        """Ensure the current song is downloaded and start playback.

        Returns:
            None.

        Side Effects:
            Downloads audio with yt-dlp, plays via FFmpeg, updates UI.

        Async:
            This function is a coroutine and must be awaited.
        """
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
                if config.YT_DLP_POT_BASE_URL:
                    ytDlpArgs += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
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
                stdout, stderr = await proc.communicate()
                self.activeDownloadProc = None

                if not self.currentSong:
                    # Download was cancelled
                    return

                if proc.returncode != 0:
                    self.isDownloading = False
                    stderrStr = stderr.decode('utf-8', errors='replace')
                    errTail = stderrStr.strip().splitlines()[-5:]
                    errMsg = " | ".join(errTail)
                    reason = _diagnoseYtDlpFailure(stderrStr, proc.returncode)
                    analytics.capture("play song failed", user=self.lastRequester, guild=guild,
                                      properties={"stage": "download", "video_id": videoId,
                                                  "title": videoTitle, "returncode": proc.returncode,
                                                  "stderr_tail": errMsg[:500], "reason": reason})
                    playLogger.error(f"[DOWNLOAD FAIL] Failed to download '{videoTitle}' (ID: {videoId}) with returncode {proc.returncode}. reason: {reason}. stderr: {errMsg}")
                    if self.initialCtx:
                        try:
                            await self.initialCtx.interaction.edit_original_response(
                                content=f"❌ Error al descargar **{videoTitle}**: {reason}",
                                view=None
                            )
                        except Exception:
                            pass
                        self.initialCtx = None
                    if self.controlMessage is not None:
                        await self.updateControlMessage(f"❌ Error al descargar {videoTitle}: {reason}")
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
                reason = _diagnoseYtDlpFailure(str(e))
                if isinstance(e, FileNotFoundError):
                    reason = f"yt-dlp no encontrado en `{config.YT_DLP_PATH}`. Revisá YT_DLP_PATH en .env."
                print(f"[PLAYER ERROR] Download exception: {e}")
                analytics.capture_exception(e, user=self.lastRequester, guild=guild,
                                            properties={"stage": "download", "video_id": videoId,
                                                        "title": videoTitle, "reason": reason})
                playLogger.error(f"[DOWNLOAD ERROR] Exception downloading '{videoTitle}': {e} → reason: {reason}")
                if self.initialCtx:
                    try:
                        await self.initialCtx.interaction.edit_original_response(
                            content=f"❌ Error al descargar **{videoTitle}**: {reason}",
                            view=None
                        )
                    except Exception:
                        pass
                    self.initialCtx = None
                if self.controlMessage is not None:
                    await self.updateControlMessage(f"❌ Error al descargar {videoTitle}: {reason}")
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
        """Handle playback completion and advance the queue.

        Args:
            error: Playback error passed by Discord (if any).

        Side Effects:
            Deletes temporary files, mutates queue/history, updates UI.

        Async:
            This function is a coroutine and must be awaited.
        """
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
            await self._leaveVoice("stop")
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
            await self._leaveVoice("queue_finished")

    async def _leaveVoice(self, reason: str):
        """Disconnect from voice after playback ends.

        Args:
            reason: Short tag for logs/analytics (e.g. "stop", "queue_finished").

        Side Effects:
            Disconnects the voice client, fires analytics, clears self.vc.

        Async:
            This function is a coroutine and must be awaited.
        """
        vc = self.vc
        if not vc:
            return
        channel = getattr(vc, "channel", None)
        channel_name = getattr(channel, "name", None)
        channel_id = getattr(channel, "id", None)
        try:
            if getattr(vc, "recording", False):
                try:
                    vc.stop_recording()
                except Exception:
                    pass
                setattr(vc, "recording", False)
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    vc.cleanup()
                except Exception:
                    pass
            playLogger.info(f"[PLAYBACK LEAVE] Disconnected from voice ({reason}).")
            try:
                analytics.capture(
                    "voice channel left",
                    user=self.lastRequester,
                    guild=self.bot.get_guild(self.guildId) if self.bot else None,
                    properties={
                        "channel_id": str(channel_id) if channel_id else None,
                        "channel_name": channel_name,
                        "trigger": f"play_{reason}",
                    },
                )
            except Exception:
                pass
        except Exception as e:
            playLogger.warning(f"[PLAYBACK LEAVE] Error disconnecting ({reason}): {e}")
        finally:
            self.vc = None

    async def predownloadQueue(self):
        """Background task that pre-downloads queued songs.

        Side Effects:
            Downloads audio files to disk and updates download state.

        Async:
            This function is a coroutine and must be awaited.
        """
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
                if config.YT_DLP_POT_BASE_URL:
                    ytDlpArgs += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
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
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    duration = time.time() - startTime
                    fileSize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    playLogger.info(f"[PRE-DOWNLOAD SUCCESS] Background downloaded '{videoTitle}' (ID: {videoId}) in {duration:.2f}s. Size: {fileSize} bytes ({fileSize / (1024*1024):.2f} MB)")
                else:
                    errTail = stderr.decode('utf-8', errors='replace').strip().splitlines()[-5:]
                    errMsg = " | ".join(errTail)
                    playLogger.warning(f"[PRE-DOWNLOAD FAIL] Failed to background download '{videoTitle}' (ID: {videoId}) with code {proc.returncode}. stderr: {errMsg}")
            except Exception as e:
                playLogger.error(f"[PRE-DOWNLOAD ERROR] Exception background downloading '{videoTitle}': {e}")
            finally:
                self.downloadingIds.discard(videoId)
                
            # Sleep slightly between downloads to avoid high CPU load/network spam
            await asyncio.sleep(1)

    def startPreDownloading(self):
        """Ensure the background pre-download task is running."""
        if not self.preDownloadTask or self.preDownloadTask.done():
            self.preDownloadTask = self.bot.loop.create_task(self.predownloadQueue())

    async def togglePausePlay(self):
        """Pause or resume playback based on current state.

        Side Effects:
            Calls pause/resume on the voice client and updates UI.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.vc:
            if self.vc.is_playing():
                self.vc.pause()
                await self.updateControlMessage()
            elif self.vc.is_paused():
                self.vc.resume()
                await self.updateControlMessage()

    async def skipSong(self):
        """Skip the current song and advance the queue.

        Side Effects:
            Stops playback and triggers queue advancement.

        Async:
            This function is a coroutine and must be awaited.
        """
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            await self.onSongFinished(None)

    async def playPrevious(self):
        """Play the previous song from history.

        Side Effects:
            Mutates queue/history and restarts playback.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not self.history:
            return
        self.isPrevious = True
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            await self.onSongFinished(None)

    async def stopPlayback(self):
        """Stop playback and clear the queue.

        Side Effects:
            Clears queue state and stops the voice client if active.

        Async:
            This function is a coroutine and must be awaited.
        """
        self.isStopping = True
        self.queue.clear()
        self.isDownloading = False
        if self.vc:
            if self.vc.is_playing() or self.vc.is_paused():
                self.vc.stop()
            else:
                await self.onSongFinished(None)

    async def updateControlMessage(self, customStatus=None):
        """Create or update the interactive control message.

        Args:
            customStatus: Optional status line override.

        Side Effects:
            Sends or edits a Discord embed with UI controls.

        Async:
            This function is a coroutine and must be awaited.
        """
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
    """Playback control buttons for the GuildPlayer UI."""
    def __init__(self, player: GuildPlayer):
        """Initialize the control view for a player.

        Args:
            player: GuildPlayer instance to control.
        """
        super().__init__(timeout=None)
        self.player = player
        self.updateButtonStates()

    def updateButtonStates(self):
        """Update button labels and disabled state based on player status."""
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
        """Handle the Previous button click."""
        await interaction.response.defer()
        await self.player.playPrevious()

    @discord.ui.button(label="⏸️ Pausar", style=discord.ButtonStyle.primary, custom_id="btn_pause_play")
    async def pausePlayButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Pause/Resume button click."""
        await interaction.response.defer()
        await self.player.togglePausePlay()

    @discord.ui.button(label="⏭️ Siguiente", style=discord.ButtonStyle.secondary, custom_id="btn_next")
    async def nextButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Next button click."""
        await interaction.response.defer()
        await self.player.skipSong()

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def stopButton(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle the Stop button click."""
        await interaction.response.defer()
        await self.player.stopPlayback()


def getGuildPlayer(guildId: int, bot) -> GuildPlayer:
    """Return or create the GuildPlayer for a guild.

    Args:
        guildId: Discord guild ID.
        bot: Discord bot client.

    Returns:
        GuildPlayer instance bound to the guild.
    """
    if guildId not in guildPlayers:
        guildPlayers[guildId] = GuildPlayer(guildId, bot)
    return guildPlayers[guildId]

def clearGuildPlayer(guildId: int):
    """Clear a GuildPlayer and delete any queued downloads.

    Args:
        guildId: Discord guild ID.

    Side Effects:
        Cancels background tasks, deletes cached audio files, and clears state.
    """
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

def _format_choice_prompt(candidates: list[dict]) -> str:
    """Render a numbered list of search candidates for the "¿cuál querés?"
    prompt. Shared shape used by the /play picker message."""
    lines = ["🎵 Encontré varias, ¿cuál querés?"]
    for i, c in enumerate(candidates, 1):
        dur = c.get("duration_string") or ""
        durSuffix = f" `[{dur}]`" if dur else ""
        lines.append(f"**{i}.** {c['title']}{durSuffix}")
    return "\n".join(lines)


class PlaySearchView(discord.ui.View):
    """Pick-one menu shown when /play finds several matches for a search.

    Only the user who ran /play may choose. Selecting an option queues that
    single song through the SAME path as a normal /play (``addSongs``), so the
    download-progress message, cancel button and per-song error reporting all
    work exactly as if the user had typed that title directly.
    """
    def __init__(self, player: "GuildPlayer", ctx, candidates: list[dict]):
        super().__init__(timeout=120)
        self.player = player
        self.ctx = ctx
        self.requester_id = getattr(getattr(ctx, "author", None), "id", None)
        self.candidates = candidates
        self.message = None

        options = []
        for i, c in enumerate(candidates):
            dur = c.get("duration_string") or ""
            label = c["title"][:100]
            desc = f"[{dur}]" if dur else None
            options.append(discord.SelectOption(label=label, value=str(i), description=desc))
        select = discord.ui.Select(
            placeholder="Elegí el tema...",
            options=options,
            custom_id="play_choice_select",
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        """Queue the chosen candidate (requester-only)."""
        if self.requester_id is not None and interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Esta elección es de quien pidió el tema 😉", ephemeral=True,
            )
            return
        try:
            idx = int(interaction.data["values"][0])
        except (KeyError, ValueError, IndexError):
            await interaction.response.defer()
            return
        if idx < 0 or idx >= len(self.candidates):
            await interaction.response.defer()
            return
        song = self.candidates[idx]
        # Ack the component interaction without editing; addSongs drives the
        # original /play response from here (replacing this menu with the
        # download-progress message + cancel button).
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await self.player.addSongs([song], self.ctx)


async def playLogic(ctx: discord.ApplicationContext, query: str):
    """Handle the /play slash command.

    Args:
        ctx: Discord application context.
        query: Search term or YouTube URL.

    Returns:
        None.

    Side Effects:
        Connects to voice, downloads audio with yt-dlp, and starts playback.

    Async:
        This function is a coroutine and must be awaited.
    """
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
    isSearch = not (inputStr.startswith("http://") or inputStr.startswith("https://") or inputStr.startswith("ytsearch:"))
    if isSearch:
        # ytsearchN para tener candidatos: los primeros hits suelen ser canales
        # (ej. "Indio Solari" devuelve el canal UCzq3uuD... que yt-dlp no
        # puede bajar como video). Despues filtramos a videos validos y, si hay
        # mas de uno, le ofrecemos al usuario que elija cual quiere.
        inputStr = f"ytsearch{_PLAY_CHOICE_COUNT + 2}:{inputStr}"

    # 1. Fetch metadata (ID and Title)
    await safeEdit(ctx, "🔍 Buscando y obteniendo metadatos...")
    try:
        cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
        ytDlpArgs = [config.YT_DLP_PATH]
        if os.path.exists(cookiesPath):
            ytDlpArgs += ["--cookies", cookiesPath]
        if config.YT_DLP_POT_BASE_URL:
            ytDlpArgs += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
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
            reason = _diagnoseYtDlpFailure(errMsg, proc.returncode)
            print(f"[PLAY ERROR] Metadata fetch failed: {reason} ({errMsg[:200]})")
            playLogger.error(f"[METADATA FAIL] Query '{query}' failed with returncode {proc.returncode}. reason: {reason}. stderr: {errMsg[:500]}")
            return await safeEdit(ctx, f"❌ Error al buscar el video: {reason}")

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

        if isSearch:
            # YouTube search mezcla videos con canales/playlists; filtramos
            # los que no son videos (id de canal "UC..." o sin duracion).
            songs = [s for s in songs if not s["id"].startswith("UC") and s["duration_string"]]

        if not songs:
            return await safeEdit(ctx, "❌ No se pudieron obtener los metadatos del video.")
    except FileNotFoundError as e:
        playLogger.error(f"[METADATA FAIL] yt-dlp binary missing at {config.YT_DLP_PATH}: {e}")
        return await safeEdit(ctx, f"❌ yt-dlp no encontrado en `{config.YT_DLP_PATH}`. Revisá YT_DLP_PATH en .env.")
    except Exception as e:
        reason = _diagnoseYtDlpFailure(str(e))
        playLogger.error(f"[METADATA FAIL] Exception during metadata fetch for '{query}': {e} → reason: {reason}")
        print(f"[PLAY ERROR] Exception during metadata fetch: {e}")
        return await safeEdit(ctx, f"❌ Error al buscar el video: {reason}")

    # Free-text search with several candidates → let the requester pick which
    # one instead of silently grabbing the first hit (which often was the wrong
    # version). A direct URL/playlist skips this and queues straight away.
    if isSearch and len(songs) > 1:
        candidates = songs[:_PLAY_CHOICE_COUNT]
        view = PlaySearchView(player, ctx, candidates)
        prompt = _format_choice_prompt(candidates)
        try:
            view.message = await ctx.interaction.edit_original_response(
                content=prompt, view=view,
            )
        except Exception:
            # Keep the picker usable on the fallback path too — a plain text
            # prompt would leave the user with options but no way to choose.
            try:
                view.message = await ctx.followup.send(prompt, view=view)
            except Exception:
                await safeEdit(ctx, prompt)
        return

    # Single result (or a URL/playlist): queue it directly.
    await player.addSongs(songs, ctx)


# ---------- Programmatic entry points (no slash ctx) -----------------------
# These let other modules (notably geminiCommand when the indio decides to
# play music or a sound) drive playback without a Discord interaction.


def _pick_voice_channel(bot, guild_id: int) -> Optional[discord.VoiceChannel]:
    """Pick the most-populated voice channel in the guild, or the channel
    the bot is already connected to. Returns None if no usable channel."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    # Prefer where the bot is already connected.
    if guild.voice_client and getattr(guild.voice_client, "channel", None):
        return guild.voice_client.channel
    candidates = []
    for ch in guild.voice_channels:
        humans = sum(1 for m in ch.members if not m.bot)
        if humans > 0:
            candidates.append((ch, humans))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1], reverse=True)
    return candidates[0][0]


async def _yt_dlp_search(query: str, *, max_results: int = 1) -> list[dict]:
    """Run yt-dlp to resolve the query to song metadata. Returns a list of
    {id, title, duration_string} dicts; empty list on any failure.

    ``max_results`` caps how many *search* hits to return (defaults to 1, the
    legacy single-pick behaviour). It only applies to free-text searches; a
    direct URL/playlist always returns every entry yt-dlp reports so playlists
    keep queueing in full. We fetch a couple extra candidates beyond
    ``max_results`` because the first hits are often channels/playlists that get
    filtered out below.
    """
    inputStr = query.strip()
    isSearch = not (inputStr.startswith("http://") or inputStr.startswith("https://")
                    or inputStr.startswith("ytsearch:"))
    if isSearch:
        # ytsearchN + filtro abajo: los primeros hits suelen ser canales
        # (ej. "Indio Solari" → UCzq3uuD...) que no se pueden bajar, así que
        # pedimos algunos de más para tener candidatos válidos suficientes.
        fetch_n = max(max_results + 2, 3)
        inputStr = f"ytsearch{fetch_n}:{inputStr}"
    cookiesPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    args = [config.YT_DLP_PATH]
    if os.path.exists(cookiesPath):
        args += ["--cookies", cookiesPath]
    if config.YT_DLP_POT_BASE_URL:
        args += ["--extractor-args",
                 f"youtubepot-bgutilhttp:base_url={config.YT_DLP_POT_BASE_URL}"]
    args += [
        "--flat-playlist", "--simulate",
        "--print", "%(id)s",
        "--print", "%(title)s",
        "--print", "%(duration_string)s",
        inputStr,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception as e:
        playLogger.warning(f"[PLAY-INDIO] yt-dlp spawn failed: {e}")
        return []
    if proc.returncode != 0:
        playLogger.warning(f"[PLAY-INDIO] yt-dlp rc={proc.returncode}: "
                           f"{stderr.decode('utf-8', 'replace').strip()[:200]}")
        return []
    lines = [l.strip() for l in stdout.decode("utf-8", "replace").strip().split("\n") if l.strip()]
    songs: list[dict] = []
    for i in range(0, len(lines) - 2, 3):
        dur = lines[i + 2]
        songs.append({
            "id": lines[i],
            "title": lines[i + 1],
            "duration_string": dur if dur != "NA" else "",
        })
    if isSearch:
        # Filtramos canales/playlists para quedarnos con videos reales.
        videos = [s for s in songs if not s["id"].startswith("UC") and s["duration_string"]]
        songs = videos[:max_results]
    return songs


async def playFromIndio(bot, guild_id: int, query: str,
                        voice_channel_id: Optional[int] = None,
                        *, songs: Optional[list[dict]] = None) -> tuple[bool, str]:
    """Queue a YouTube search/URL programmatically — no slash ctx required.

    Used by the indio when someone asks him to play music. Picks a voice
    channel automatically, but the text channel for status + GuildPlayer
    control panel is always ``config.INDIO_PLAY_CHANNEL_ID`` (no fallback);
    if that channel is missing the action fails.

    ``songs`` lets a caller pass an already-resolved list of
    ``{id, title, duration_string}`` dicts (e.g. the candidate the user picked
    from a disambiguation menu) so we skip the yt-dlp search entirely. When it
    is ``None`` we search using ``query`` as before.

    Returns:
        (ok, message): ``ok=True`` if playback started or song queued;
        the message is a short user-facing status.
    """
    if not query or not query.strip():
        return False, "query vacio"

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False, "guild no encontrado"

    voice_channel = None
    if voice_channel_id:
        ch = guild.get_channel(int(voice_channel_id))
        if isinstance(ch, discord.VoiceChannel):
            voice_channel = ch
    if voice_channel is None:
        voice_channel = _pick_voice_channel(bot, guild_id)
    if voice_channel is None:
        return False, "no hay nadie en un canal de voz para reproducir"

    text_channel = guild.get_channel(config.INDIO_PLAY_CHANNEL_ID)
    if text_channel is None or not hasattr(text_channel, "send"):
        playLogger.warning(
            "[PLAY-INDIO] INDIO_PLAY_CHANNEL_ID=%s no encontrado en guild %s",
            config.INDIO_PLAY_CHANNEL_ID, guild_id,
        )
        return False, (f"no encuentro el canal de musica configurado "
                       f"(id={config.INDIO_PLAY_CHANNEL_ID})")

    vc = guild.voice_client
    try:
        if vc is None or not vc.is_connected():
            set_pending_trigger(voice_channel.id, bot.user.id if bot.user else 0)
            vc = await voice_channel.connect(reconnect=True)
        elif vc.channel.id != voice_channel.id:
            if getattr(vc, "recording", False):
                try:
                    vc.stop_recording()
                except Exception:
                    pass
                setattr(vc, "recording", False)
            set_pending_trigger(voice_channel.id, bot.user.id if bot.user else 0)
            await vc.move_to(voice_channel)
    except Exception as e:
        playLogger.warning(f"[PLAY-INDIO] voice connect failed: {e}")
        return False, f"no pude conectarme a voz: {e}"

    if songs is None:
        songs = await _yt_dlp_search(query)
    if not songs:
        return False, "no encontre nada en YouTube con esa busqueda"

    player = getGuildPlayer(guild_id, bot)
    player.vc = vc
    player.textChannel = text_channel

    isFirst = (not player.currentSong and len(player.queue) == 0)
    title = songs[0]["title"]
    note = f"🎶 **{title}** {'arrancando' if isFirst else 'a la cola'} (pedido al indio)."
    try:
        await text_channel.send(note)
    except Exception:
        playLogger.exception("[PLAY-INDIO] failed to post note in sick-tunes")

    try:
        await player._enqueueAndMaybeStart(songs, source="indio")
    except Exception as e:
        playLogger.exception("[PLAY-INDIO] enqueue/start failed")
        return False, f"falló el inicio: {e}"

    return True, title


async def playSoundFromIndio(bot, guild_id: int, sound_query: str) -> tuple[bool, str]:
    """Play a local sound clip programmatically. Skips if music is currently
    playing — the indio shouldn't step on the music. ``sound_query`` is a
    fuzzy match against filenames under ``config.CUSTOM_AUDIO_PATH``.

    Returns:
        (ok, message).
    """
    if not sound_query or not sound_query.strip():
        return False, "sound query vacio"

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False, "guild no encontrado"

    # Refuse if music is playing.
    player = guildPlayers.get(guild_id)
    if player is not None and player.currentSong:
        return False, "hay musica sonando, no toco el soundpad"
    vc = guild.voice_client
    if vc and vc.is_playing():
        return False, "vapls ya esta reproduciendo algo, paso"

    # Locate the sound file under CUSTOM_AUDIO_PATH (recursive fuzzy match).
    root = getattr(config, "CUSTOM_AUDIO_PATH", None) or getattr(config, "AUDIO_DIR", None)
    if not root or not os.path.isdir(root):
        return False, "CUSTOM_AUDIO_PATH no configurado"
    needle = sound_query.strip().lower()
    matches: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
                continue
            if needle in f.lower():
                matches.append(os.path.join(dirpath, f))
    if not matches:
        return False, f"no encontre un sonido que matchee '{sound_query}'"
    matches.sort(key=lambda p: len(os.path.basename(p)))
    filepath = matches[0]

    voice_channel = _pick_voice_channel(bot, guild_id)
    if voice_channel is None:
        return False, "no hay nadie en voz para reproducir el sonido"

    try:
        if vc is None or not vc.is_connected():
            vc = await voice_channel.connect(reconnect=True)
        elif vc.channel.id != voice_channel.id:
            await vc.move_to(voice_channel)
    except Exception as e:
        return False, f"no pude conectarme a voz: {e}"

    try:
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.2)
        vc.play(discord.FFmpegOpusAudio(filepath))
    except Exception as e:
        playLogger.exception("[PLAY-INDIO] sound playback failed")
        return False, f"falló la reproduccion: {e}"

    return True, os.path.basename(filepath)
