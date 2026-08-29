"""GoLive userbot: IPTV + Instagram streaming via a dedicated Discord user account.

Runs separately from the indio userbot. No voice receive, no Whisper,
no VOSK, no DAVE — just FFmpeg → H.264 → RTP out a Discord UDP socket.

Endpoints:
  POST /stream     — start an IPTV Go Live in a voice channel
  POST /stopstream — stop the active stream
  POST /stream/control — pause/resume/seek the active stream
  POST /instagram  — start an infinite Instagram Reel stream (Go Live)
"""

import asyncio
import json
import logging
import os
import time
import sys
from typing import Optional

import aiohttp
from aiohttp import web
import discord
import discord.gateway

import config
import video_compat as vc
import davey_compat
try:
    from golive.slopsoil.golive import GoLiveConnection
except ModuleNotFoundError:
    try:
        from slopsoil.golive import GoLiveConnection
    except ModuleNotFoundError:
        from golive_connection import GoLiveConnection



# Must patch before any voice connections (before client.start())
vc.patch_video(discord.gateway)

import discord.voice_state
discord.voice_state.davey = davey_compat
discord.gateway.davey = davey_compat
davey_compat.patch_reinit(discord.voice_state)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("golive")

logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)

client = discord.Client(chunk_guilds_at_startup=False)

class GoLiveStream:
    def __init__(self, bot, guild_id, channel_id, vc, url):
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.vc = vc
        self.url = url
        self.conn = None
        self.video_player = None
        self.audio_sender = None
        self.video_ssrc = None
        self.is_live = True
        self.target_url = None
        self.title = None
        self.reconnect_attempts = 0
        self._stopped = False
        self._inactivity_task = None
        self.queue = []
        self.queue_titles = []
        self.idle_event = None
        self.is_first_reel = ("instagram.com" in url)

        # RTP counters carried across reel transitions so the client's jitter
        # buffer never sees the video clock jump backwards on the same SSRC.
        self._video_seq: int = 0
        self._video_ts: int = 0
        self._audio_seq: int = 0
        self._audio_ts: int = 0
        # Background yt-dlp prefetch of the next queued reel: url -> (target_url,
        # title, is_live), plus the set of urls currently being resolved.
        self._prefetch_cache: dict[str, tuple[str, str, bool]] = {}
        self._prefetch_urls: set[str] = set()

    async def start(self):
        from ytdlp import _yt_extract_url
        
        target_url = self.url
        title = "Stream"
        is_live = True
        
        if target_url.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            _path = urlparse(target_url).path.lower()
            if _path.endswith((".m3u8", ".mpd", ".m3u")) or ".m3u8" in target_url.lower():
                log.info("[STREAM] Direct stream URL detected — skipping yt-dlp")
            else:
                log.info("[STREAM] Checking stream URL via yt-dlp for %s", target_url)
                try:
                    res = await _yt_extract_url(target_url)
                except Exception as e:
                    log.warning("[STREAM] yt-dlp extraction failed: %s", e)
                    raise RuntimeError(f"Failed to extract stream URL via yt-dlp: {e}")

                if res:
                    target_url, title, self.is_live = res
                    log.info("[STREAM] Extracted stream: %s -> %s (live=%s)", title, target_url, self.is_live)
                else:
                    raise RuntimeError("Failed to extract stream URL via yt-dlp")
        
        self.target_url = target_url
        self.title = title

        log.info("[STREAM] Establishing GoLive connection...")
        
        async def _dummy_send(msg, **kwargs):
            log.info("[STREAM_STATUS] %s", msg)

        guild = client.get_guild(self.guild_id)
        channel = guild.get_channel(self.channel_id) if guild else None
        if guild and not self.vc:
            self.vc = _vc_for_guild(guild)

        if self.is_first_reel:
            log.info("[STREAM] First Instagram Reel detected. Pausing 5 seconds for viewers to connect...")
            await asyncio.sleep(5.0)
            self.is_first_reel = False

        try:
            await slopsoil_start_live_stream(
                bot=client,
                send=_dummy_send,
                guild=guild,
                voice_channel=channel,
                vc=self.vc,
                title=self.title,
                url=self.target_url,
                live=self.is_live,
                audio=True,
            )
            self.conn = getattr(client, "live_connections", {}).get(self.guild_id)
            self.video_player = getattr(client, "video_players", {}).get(self.guild_id)
            self.video_ssrc = (self.conn.ssrc + 1) if (self.conn and hasattr(self.conn, "ssrc")) else 101
        except Exception as e:
            log.warning("[STREAM] slopsoil_start_live_stream fallback to direct connection: %s", e)
            try:
                from golive.slopsoil.golive import GoLiveConnection
            except ModuleNotFoundError:
                try:
                    from slopsoil.golive import GoLiveConnection
                except ModuleNotFoundError:
                    from golive_connection import GoLiveConnection
            self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
            conn_res = self.conn.connect(timeout=30.0)
            if asyncio.iscoroutine(conn_res) or hasattr(conn_res, "__await__"):
                await conn_res
            self.video_ssrc = self.conn.ssrc + 1
            
            await self._start_players()

        self._inactivity_task = asyncio.create_task(self._inactivity_loop())

    async def _start_players(self):
        from streamer import H264VideoPlayer, _stream_fps
        try:
            from golive_connection import _GoLiveVCProxy, GoLiveAudioSender
        except ModuleNotFoundError:
            try:
                from golive.slopsoil.golive import _GoLiveVCProxy, GoLiveAudioSender
            except ModuleNotFoundError:
                from slopsoil.golive import _GoLiveVCProxy, GoLiveAudioSender
        proxy_vc = _GoLiveVCProxy(self.conn)
        if self._video_ts or self._audio_ts:
            log.info(
                "[STREAM] Seeding RTP: video seq=%d ts=%d, audio seq=%d ts=%d",
                self._video_seq, self._video_ts, self._audio_seq, self._audio_ts,
            )
        self.video_player = H264VideoPlayer(
            url=self.target_url,
            voice_client=proxy_vc,
            fps=_stream_fps(),
            live=self.is_live,
            audio=True,
            original_url=self.url,
            initial_seq=self._video_seq,
            initial_ts=self._video_ts,
        )
        self.video_player.start()
        log.info("[STREAM] Video player started for '%s'", self.title)
        
        log.info("[STREAM] Waiting for audio FIFO...")
        try:
            f = await asyncio.wait_for(
                asyncio.to_thread(open, self.video_player.audio_fifo, "rb"),
                timeout=30.0,
            )
        except TimeoutError:
            log.error("[STREAM] Timed out waiting for audio FIFO")
            raise RuntimeError("Timed out waiting for audio FIFO")
            
        self.audio_sender = GoLiveAudioSender(
            file_obj=f,
            conn=self.conn,
            is_source_active=self.video_player.is_source_active,
            initial_seq=self._audio_seq,
            initial_ts=self._audio_ts,
        )
        self.audio_sender.start()
        log.info("[STREAM] Audio sender started for '%s'", self.title)

    async def _stop_players(self):
        if self.video_player:
            self.video_player.stop()
        if self.audio_sender:
            self.audio_sender.stop()
        
        await asyncio.to_thread(self._wait_players)
        
        # Snapshot the RTP counters before discarding the players so the next
        # reel's players continue the same seq/ts on the shared SSRC.
        vp = self.video_player
        if vp is not None:
            s = getattr(vp, "_seq", None)
            t = getattr(vp, "_ts", None)
            if isinstance(s, int):
                self._video_seq = s & 0xFFFF
            if isinstance(t, int):
                self._video_ts = t & 0xFFFF_FFFF
        ap = self.audio_sender
        if ap is not None:
            s = getattr(ap, "_seq", None)
            t = getattr(ap, "_ts", None)
            if isinstance(s, int):
                self._audio_seq = s & 0xFFFF
            if isinstance(t, int):
                self._audio_ts = t & 0xFFFF_FFFF

        self.video_player = None
        self.audio_sender = None

    def _wait_players(self):
        deadline = time.monotonic() + 5.0
        for p in (self.video_player, self.audio_sender):
            if p and p.is_alive():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    p.join(timeout=remaining)
                if p.is_alive():
                    log.warning('[STREAM] %s still alive after 5s', p.name)

    async def _prefetch_next(self) -> None:
        """Resolve the next queued reel's stream URL in the background so the
        reel-to-reel transition has no yt-dlp wait."""
        if self._stopped or not self.queue:
            return
        from ytdlp import _yt_extract_url
        url = self.queue[0]
        if url in self._prefetch_cache or url in self._prefetch_urls:
            return
        self._prefetch_urls.add(url)
        try:
            res = await _yt_extract_url(url)
            if res and not self._stopped:
                self._prefetch_cache[url] = res
        except Exception as e:
            log.warning("[STREAM] prefetch failed for %s: %s", url, e)
        finally:
            self._prefetch_urls.discard(url)

    async def _inactivity_loop(self):
        """Monitors player health and reconnects if live, or auto-stops."""
        disconnect_voice = True
        try:
            while not self._stopped:
                await asyncio.sleep(0.5)
                if self._stopped:
                    break

                if self.conn and not self.conn.healthy:
                    log.warning("[STREAM] GoLive connection lost. Reconnecting...")
                    ok = await self._restart_connection()
                    if self._stopped:
                        break
                    if not ok:
                        log.error("[STREAM] GoLive reconnect failed. Auto-stopping.")
                        break
                    continue

                if self.video_player and not self.video_player.is_alive():
                    if self.is_live:
                        self.reconnect_attempts += 1
                        emitted = getattr(self.video_player, "_frames_emitted", 0)
                        if emitted > 900:  # ~30s at 30fps means stable stream
                            self.reconnect_attempts = 0

                        if self.reconnect_attempts > 5:
                            log.error("[STREAM] Video player died too many times. Auto-stopping.")
                            break

                        log.warning("[STREAM] Video player died (attempt %d). Reconnecting in 3s...", self.reconnect_attempts)
                        await self._stop_players()
                        await asyncio.sleep(3)
                        if self._stopped:
                            break
                        try:
                            await self._start_players()
                        except Exception as e:
                            log.error("[STREAM] Failed to restart players: %s", e)
                            break
                    else:
                        log.info("[STREAM] Video player ended naturally.")
                        if self.queue:
                            next_url = self.queue.pop(0)
                            next_title = self.queue_titles.pop(0)
                            log.info("[STREAM] Playing next queued video: %s (%s)", next_url, next_title)
                            
                            await self._stop_players()
                            
                            # Resolve target URL via yt-dlp (prefetched while the
                            # previous reel was playing; inline fallback otherwise).
                            res = self._prefetch_cache.pop(next_url, None)
                            if res is None:
                                from ytdlp import _yt_extract_url
                                try:
                                    res = await _yt_extract_url(next_url)
                                except Exception as e:
                                    log.error("[STREAM] yt-dlp extraction failed for next URL: %s", e)
                                    continue
                            if res:
                                self.target_url, self.title, self.is_live = res
                                self.url = next_url
                            else:
                                log.error("[STREAM] yt-dlp failed to extract next URL")
                                continue
                            
                            # Set nickname to reflect new video
                            guild = client.get_guild(self.guild_id)
                            if guild:
                                await _set_nickname(guild, f"GoLive - {next_title}")
                            
                            await self._start_players()
                            if self.queue:
                                asyncio.create_task(self._prefetch_next())
                        else:
                            log.info("[STREAM] No more videos in queue. Entering idle state for 60 seconds...")
                            await self._stop_players()
                            
                            self.idle_event = asyncio.Event()
                            try:
                                await asyncio.wait_for(self.idle_event.wait(), timeout=60.0)
                                log.info("[STREAM] Woken up from idle state by new queued video!")
                                continue
                            except asyncio.TimeoutError:
                                log.info("[STREAM] Idle timeout reached. Closing stream.")
                                disconnect_voice = False
                                break
        except asyncio.CancelledError:
            return

        if not self._stopped:
            _active_streams.pop(self.guild_id, None)
            await self.stop(disconnect_voice=disconnect_voice)

    async def _restart_connection(self) -> bool:
        """Re-establish the GoLive connection from scratch after a WS/UDP drop.
        Returns True on success, False on failure or exhausted retries."""
        if self._stopped:
            return False
        self.reconnect_attempts += 1
        if self.reconnect_attempts > 5:
            log.error("[STREAM] GoLive reconnect too many times. Auto-stopping.")
            return False

        await self._stop_players()

        if self.conn:
            try:
                await self.conn.disconnect()
            except Exception:
                pass

        await asyncio.sleep(3)
        if self._stopped:
            return False

        try:
            self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
            await self.conn.connect(timeout=30.0)
            self.video_ssrc = self.conn.ssrc + 1
            await self._start_players()
            log.info("[STREAM] GoLive reconnected successfully")
            return True
        except Exception as e:
            log.error("[STREAM] GoLive reconnect failed: %s", e)
            return False

    def pause(self):
        if self.video_player:
            self.video_player.pause()

    def resume(self):
        if self.video_player:
            self.video_player.resume()

    def seek(self, target_sec: float):
        if self.video_player:
            self.video_player.seek(target_sec)

    async def stop(self, disconnect_voice: bool = False):
        if self._stopped:
            return
        self._stopped = True

        if self._inactivity_task:
            self._inactivity_task.cancel()
            self._inactivity_task = None

        # 1. Stop streaming / screenshare immediately (STREAM_DELETE)
        if self.conn:
            log.info("[STREAM] Stopping GoLive screenshare transmission...")
            try:
                await asyncio.wait_for(self.conn.disconnect(), timeout=2.0)
            except Exception as e:
                log.warning("[STREAM] GoLive disconnect failed: %s", e)

        # 2. Disconnect from voice channel if requested (e.g. by watchdog or stop command)
        if disconnect_voice and self.vc:
            try:
                if hasattr(self.vc, "_connection") and self.vc.is_connected():
                    log.info("[STREAM] Disconnecting voice client...")
                    await asyncio.wait_for(
                        self.vc._connection.disconnect(force=True, wait=False),
                        timeout=3.0
                    )
                    self.vc.cleanup()
                    log.info("[STREAM] VoiceClient disconnected and cleaned up")
                elif self.vc.is_connected():
                    await asyncio.wait_for(self.vc.disconnect(force=True), timeout=3.0)
                    log.info("[STREAM] VoiceClient disconnected gracefully")
            except Exception as e:
                log.warning("[STREAM] VoiceClient disconnect failed: %s", e)

        log.info("[STREAM] Cleaning up players...")
        try:
            await self._stop_players()
        except Exception:
            pass

        guild = client.get_guild(self.guild_id)
        if guild:
            _schedule_nickname_restore(guild)

        log.info("[STREAM] Stream stopped and cleaned up")


class HeadbanzGoLiveStream:
    """Manages GoLive streaming of a static image for the Headbanz game."""
    def __init__(self, bot, guild_id: int, channel_id: int, vc, image_path: str) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.vc = vc
        self.image_path = image_path
        self.conn = None
        self.video_player = None
        self.audio_sender = None
        self.video_ssrc = None
        self.is_live = False
        self._stopped = False
        self._inactivity_task = None

    async def start(self) -> None:
        log.info("[HEADBANZ] Establishing GoLive connection...")
        self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
        await self.conn.connect(timeout=30.0)
        self.video_ssrc = self.conn.ssrc + 1
        await self._start_players()
        self._inactivity_task = asyncio.create_task(self._inactivity_loop())

    async def _start_players(self) -> None:
        from streamer import HeadbanzPlayer
        try:
            from golive_connection import _GoLiveVCProxy, GoLiveAudioSender
        except ModuleNotFoundError:
            try:
                from golive.slopsoil.golive import _GoLiveVCProxy, GoLiveAudioSender
            except ModuleNotFoundError:
                from slopsoil.golive import _GoLiveVCProxy, GoLiveAudioSender
        proxy_vc = _GoLiveVCProxy(self.conn)
        self.video_player = HeadbanzPlayer(
            image_path=self.image_path,
            voice_client=proxy_vc,
            fps=15.0,
        )
        self.video_player.start()
        log.info("[HEADBANZ] Headbanz video player started")

        log.info("[HEADBANZ] Waiting for audio FIFO...")
        try:
            f = await asyncio.wait_for(
                asyncio.to_thread(open, self.video_player.audio_fifo, "rb"),
                timeout=15.0,
            )
        except TimeoutError:
            log.error("[HEADBANZ] Timed out waiting for audio FIFO")
            raise RuntimeError("Timed out waiting for audio FIFO")

        self.audio_sender = GoLiveAudioSender(
            file_obj=f,
            conn=self.conn,
            is_source_active=self.video_player.is_source_active,
        )
        self.audio_sender.start()
        log.info("[HEADBANZ] Audio sender started")

    async def _stop_players(self) -> None:
        if self.video_player:
            self.video_player.stop()
        if self.audio_sender:
            self.audio_sender.stop()
        await asyncio.to_thread(self._wait_players)
        self.video_player = None
        self.audio_sender = None

    def _wait_players(self) -> None:
        deadline = time.monotonic() + 5.0
        for p in (self.video_player, self.audio_sender):
            if p and p.is_alive():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    p.join(timeout=remaining)
                if p.is_alive():
                    log.warning("[HEADBANZ] %s still alive after 5s", p.name)

    async def _inactivity_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(2)
                if self._stopped:
                    break
                if self.conn and not self.conn.healthy:
                    log.warning("[HEADBANZ] GoLive connection lost. Auto-stopping.")
                    break
                if self.video_player and not self.video_player.is_alive():
                    log.info("[HEADBANZ] Video player ended naturally — auto-stopping")
                    break
        except asyncio.CancelledError:
            return
        if not self._stopped:
            _active_streams.pop(self.guild_id, None)
            await self.stop()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._inactivity_task:
            self._inactivity_task.cancel()
            self._inactivity_task = None
        log.info("[HEADBANZ] Stopping stream...")
        try:
            await self._stop_players()
        except Exception:
            pass
        if self.conn:
            try:
                await asyncio.wait_for(self.conn.disconnect(), timeout=5.0)
            except Exception:
                pass
        if self.vc and self.vc.is_connected():
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
        guild = client.get_guild(self.guild_id)
        if guild:
            _schedule_nickname_restore(guild)
        log.info("[HEADBANZ] Stream stopped and cleaned up")


_active_streams: dict[int, GoLiveStream] = {}
_nick_restore_tasks: dict[int, asyncio.Task] = {}

DEFAULT_NICKNAME = "GoLive - VaPls"


async def _set_nickname(guild: discord.Guild, name: str) -> None:
    """Change the golive bot's nickname in a guild (32-char Discord limit)."""
    if os.getenv("ENABLE_NICKNAME_CHANGE", "true").lower() not in ("1", "true", "yes"):
        log.info("[NICK] skipped because ENABLE_NICKNAME_CHANGE=false")
        return
    nick = name[:32]
    try:
        me = getattr(guild, "me", None) or guild.get_member(client.user.id)
        if me is None:
            me = await guild.fetch_member(client.user.id)
        if me is not None:
            await me.edit(nick=nick)
            log.info("[NICK] set to '%s' in guild=%s", nick, guild.id)
        else:
            log.warning("[NICK] bot member not found in guild=%s", guild.id)
    except Exception as e:
        log.warning("[NICK] failed to set nick in guild=%s: %s", guild.id, e)


def _schedule_nickname_restore(guild: discord.Guild) -> None:
    async def _delayed_restore():
        await asyncio.sleep(30)
        await _set_nickname(guild, DEFAULT_NICKNAME)
        _nick_restore_tasks.pop(guild.id, None)

    task = _nick_restore_tasks.get(guild.id)
    if task:
        task.cancel()
    _nick_restore_tasks[guild.id] = asyncio.create_task(_delayed_restore())


def _guild_allowed(guild_id: int) -> bool:
    return config.GUILD_ALLOWLIST is None or guild_id in config.GUILD_ALLOWLIST


def _vc_for_guild(guild: discord.Guild) -> Optional[discord.VoiceClient]:
    for vc in client.voice_clients:
        if vc.guild.id == guild.id:
            return vc  # type: ignore[return-value]
    return None


async def _join_channel(channel: discord.VoiceChannel):
    if not _guild_allowed(channel.guild.id):
        return
    if not client.is_ready():
        log.info("[VOICE] client not ready, waiting up to 30s...")
        for _ in range(30):
            if client.is_ready():
                break
            await asyncio.sleep(1)
        if not client.is_ready():
            log.warning("[VOICE] client still not ready after 30s")
            return
    existing = _vc_for_guild(channel.guild)
    try:
        if existing:
            if existing.channel.id == channel.id and existing.is_connected():
                vc = existing
            else:
                log.info(
                    "[VOICE] Reconnecting: %s → %s", existing.channel.name, channel.name
                )
                try:
                    await existing.disconnect(force=True)
                except Exception as e:
                    log.warning("[VOICE] disconnect error (ignored): %s", e)
                await asyncio.sleep(0.5)
                vc = await channel.connect(reconnect=True, timeout=20.0)
        else:
            log.info("[VOICE] Connecting to %s (%s)", channel.name, channel.guild.name)
            if channel.guild.me and getattr(channel.guild.me, "voice", None) and channel.guild.me.voice.channel:
                log.info("[VOICE] Resetting stale voice state in %s before connect", channel.guild.me.voice.channel.name)
                try:
                    await channel.guild.change_voice_state(channel=None)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning("[VOICE] Stale state reset error (ignored): %s", e)
            vc = await channel.connect(reconnect=True, timeout=20.0)
    except Exception as e:
        log.exception("[VOICE] Failed to join %s: %s", channel.name, e)
        return

    log.info("[VOICE] Connected: %s", vc)


# ---------- Relay handlers --------------------------------------------------


async def _relay_stream(request: web.Request) -> web.Response:
    log.info("[STREAM] request from %s", request.remote)
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])
        url = str(data["url"]).strip()
    except Exception as e:
        log.warning("[STREAM] invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)
    if not url:
        log.warning("[STREAM] empty url")
        return web.json_response({"error": "empty url"}, status=400)
    stream_title = str(data.get("channel_name", "")).strip() or "Stream"
    log.info("[STREAM] guild=%s channel=%s url=%s title=%s", guild_id, channel_id, url[:120], stream_title)

    if not client.is_ready():
        return web.json_response({"error": "client not ready"}, status=503)

    existing = _active_streams.get(guild_id)
    if existing and not getattr(existing, "_stopped", True):
        is_existing_reel = "instagram.com" in (getattr(existing, "url", "") or "")
        is_new_reel = "instagram.com" in url
        if is_existing_reel or is_new_reel or hasattr(existing, "queue"):
            if not hasattr(existing, "queue"):
                existing.queue = []
            if not hasattr(existing, "queue_titles"):
                existing.queue_titles = []
            existing.queue.append(url)
            existing.queue_titles.append(stream_title)
            log.info("[STREAM] Queued reel for guild=%s: %s (pos %d)", guild_id, url, len(existing.queue))
            if len(existing.queue) == 1 and hasattr(existing, "_prefetch_next"):
                asyncio.create_task(existing._prefetch_next())
            return web.json_response({
                "queued": True,
                "position": len(existing.queue),
                "guild_id": guild_id
            })

    guild = client.get_guild(guild_id)
    if guild is None:
        log.warning("[STREAM] guild not found: %s", guild_id)
        return web.json_response({"error": "guild not found"}, status=404)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not isinstance(channel, discord.VoiceChannel):
        return web.json_response({"error": "not a voice channel"}, status=400)
    if not _guild_allowed(guild.id):
        return web.json_response({"error": "guild not allowed"}, status=403)

    if not hasattr(client, "stream_tasks"):
        client.stream_tasks = {}
    if not hasattr(client, "video_players"):
        client.video_players = {}
    if not hasattr(client, "live_connections"):
        client.live_connections = {}

    await _join_channel(channel)
    vc = _vc_for_guild(guild)

    if stream_title:
        task = _nick_restore_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        await _set_nickname(guild, f"GoLive - {stream_title}")

    stream = GoLiveStream(client, guild_id, channel_id, vc, url)
    try:
        await stream.start()
    except Exception as e:
        log.exception("[STREAM] failed to start stream via Slopsoil engine")
        await stream.stop()
        return web.json_response({"error": str(e)}, status=500)

    _active_streams[guild_id] = stream
    log.info("[STREAM] started guild=%s channel=%s engine=slopsoil", guild_id, channel.name)
    return web.json_response({
        "started": True,
        "guild_id": guild_id,
        "channel_name": channel.name,
        "video_ssrc": stream.video_ssrc,
        "is_live": stream.is_live,
        "engine": "slopsoil_native"
    })


async def _relay_headbanz(request: web.Request) -> web.Response:
    log.info("[HEADBANZ] request from %s", request.remote)
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])
        image_path = str(data["image_path"]).strip()
    except Exception as e:
        log.warning("[HEADBANZ] invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)

    if not client.is_ready():
        return web.json_response({"error": "client not ready"}, status=503)

    existing = _active_streams.get(guild_id)
    if existing and not getattr(existing, "_stopped", True):
        log.warning("[HEADBANZ] already streaming for guild=%s", guild_id)
        return web.json_response({"error": "busy", "message": "Already streaming"}, status=409)

    guild = client.get_guild(guild_id)
    if guild is None:
        return web.json_response({"error": "guild not found"}, status=404)

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            return web.json_response({"error": f"channel not found: {e}"}, status=404)

    if not isinstance(channel, discord.VoiceChannel):
        return web.json_response({"error": "not a voice channel"}, status=400)

    await _join_channel(channel)
    vc = _vc_for_guild(guild)
    if vc is None or not vc.is_connected():
        return web.json_response({"error": "not connected"}, status=500)

    stream = HeadbanzGoLiveStream(client, guild_id, channel_id, vc, image_path)
    try:
        await stream.start()
    except Exception as e:
        await stream.stop()
        return web.json_response({"error": str(e)}, status=500)

    _active_streams[guild_id] = stream
    await _set_nickname(guild, "GoLive - Headbanz")
    return web.json_response(
        {
            "started": True,
            "guild_id": guild_id,
            "channel_name": channel.name,
            "video_ssrc": stream.video_ssrc,
            "is_live": False,
        }
    )


async def _relay_stopstream(request: web.Request) -> web.Response:
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
    except Exception as e:
        log.warning("[STOPSTREAM] invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)

    try:
        slopsoil_cancel_live_stream(client, guild_id)
    except Exception as e:
        log.warning("[STOPSTREAM] slopsoil_cancel_live_stream error: %s", e)

    stream = _active_streams.pop(guild_id, None)
    if stream:
        try:
            await stream.stop()
        except Exception as e:
            log.warning("[STOPSTREAM] stream stop failed: %s", e)

    log.info("[STOPSTREAM] stopped guild=%s", guild_id)
    return web.json_response({"stopped": True, "guild_id": guild_id})


async def _relay_stream_control(request: web.Request) -> web.Response:
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        action = data.get("action")
    except Exception as e:
        return web.json_response({"error": "invalid body"}, status=400)

    stream = _active_streams.get(guild_id)
    vp = getattr(client, "video_players", {}).get(guild_id)
    conn = getattr(client, "live_connections", {}).get(guild_id)

    if stream is None and vp is None and conn is None:
        return web.json_response({"error": "no active stream"}, status=404)

    if action == "pause":
        if stream and hasattr(stream, "pause"):
            stream.pause()
        elif vp and hasattr(vp, "pause"):
            vp.pause()
        return web.json_response({"status": "paused", "guild_id": guild_id})
    elif action == "resume":
        if stream and hasattr(stream, "resume"):
            stream.resume()
        elif vp and hasattr(vp, "resume"):
            vp.resume()
        return web.json_response({"status": "resumed", "guild_id": guild_id})
    elif action == "seek":
        try:
            target_sec = float(data.get("timestamp", 0))
            if stream and hasattr(stream, "seek"):
                stream.seek(target_sec)
            elif vp and hasattr(vp, "seek"):
                vp.seek(target_sec)
            return web.json_response({"status": "seeked", "timestamp": target_sec, "guild_id": guild_id})
        except ValueError:
            return web.json_response({"error": "invalid timestamp"}, status=400)
    elif action == "status":
        if stream:
            return web.json_response({
                "exists": True,
                "stopped": getattr(stream, "_stopped", False),
                "is_live": getattr(stream, "is_live", True),
                "video_player": str(getattr(stream, "video_player", None)),
                "video_player_alive": stream.video_player.is_alive() if getattr(stream, "video_player", None) else None,
                "audio_sender": str(getattr(stream, "audio_sender", None)),
                "audio_sender_alive": stream.audio_sender.is_alive() if getattr(stream, "audio_sender", None) else None,
                "conn_healthy": stream.conn.healthy if getattr(stream, "conn", None) and hasattr(stream.conn, "healthy") else None,
            })
        elif vp or conn:
            return web.json_response({
                "exists": True,
                "stopped": False,
                "is_live": True,
                "video_player": str(vp),
                "video_player_alive": vp.is_alive() if vp else False,
                "conn_healthy": conn.healthy if conn and hasattr(conn, "healthy") else True,
            })
        return web.json_response({"exists": False})


# ---------- Idle Watchdog ---------------------------------------------------
_idle_watchdogs: dict[int, asyncio.Task] = {}


async def _idle_watcher(guild_id: int):
    log.info("[WATCHDOG] Started idle watchdog for guild=%s", guild_id)
    idle_since = time.monotonic()
    try:
        while True:
            await asyncio.sleep(2)

            # Check if there is an active stream in this guild
            has_stream = (guild_id in _active_streams) or (hasattr(client, "live_connections") and guild_id in client.live_connections) or (hasattr(client, "video_players") and guild_id in client.video_players)

            # Find the voice client
            vc = None
            for v in client.voice_clients:
                if v.guild.id == guild_id:
                    vc = v
                    break

            if vc is None or not vc.is_connected():
                log.info("[WATCHDOG] Voice client not connected anymore, stopping watchdog for guild=%s", guild_id)
                break

            if has_stream:
                # Active streaming, reset idle timer
                idle_since = time.monotonic()
            else:
                elapsed = time.monotonic() - idle_since
                # Use a default timeout of 60 seconds
                timeout = 60.0
                if elapsed >= timeout:
                    log.info("[WATCHDOG] Guild=%s idle for %.0fs, disconnecting voice client...", guild_id, elapsed)
                    try:
                        if hasattr(vc, "_connection"):
                            await asyncio.wait_for(
                                vc._connection.disconnect(force=True, wait=False),
                                timeout=5.0
                            )
                            vc.cleanup()
                        else:
                            await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
                    except Exception as e:
                        log.warning("[WATCHDOG] Disconnect failed: %s", e)
                    break
    except asyncio.CancelledError:
        pass
    finally:
        _idle_watchdogs.pop(guild_id, None)


def _start_idle_watchdog(guild_id: int):
    _stop_idle_watchdog(guild_id)
    _idle_watchdogs[guild_id] = asyncio.create_task(_idle_watcher(guild_id))


def _stop_idle_watchdog(guild_id: int):
    task = _idle_watchdogs.pop(guild_id, None)
    if task and not task.done():
        task.cancel()
        log.info("[WATCHDOG] Stopped idle watchdog for guild=%s", guild_id)


# ---------- Events ----------------------------------------------------------


@client.event
async def on_ready():
    log.info("GoLive online as %s (id=%s)", client.user, client.user.id)


@client.event
async def on_voice_state_update(member, before, after):
    if member.id == client.user.id:
        if after.channel is not None:
            # Joined voice
            _start_idle_watchdog(after.channel.guild.id)
        else:
            # Left voice
            if before.channel is not None:
                _stop_idle_watchdog(before.channel.guild.id)


# ---------- Main ------------------------------------------------------------


try:
    from slopsoil.engine import start_live_stream as slopsoil_start_live_stream, cancel_live_stream as slopsoil_cancel_live_stream
except ModuleNotFoundError:
    from golive.slopsoil.engine import start_live_stream as slopsoil_start_live_stream, cancel_live_stream as slopsoil_cancel_live_stream


async def _start_relay() -> Optional[web.AppRunner]:
    if not config.RELAY_SECRET:
        log.warning("RELAY_SECRET not set — HTTP relay disabled.")
        return None
    app = web.Application()
    app.router.add_post("/stream", _relay_stream)
    app.router.add_post("/stopstream", _relay_stopstream)
    app.router.add_post("/stream/control", _relay_stream_control)
    app.router.add_post("/headbanz", _relay_headbanz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.RELAY_HOST, port=config.RELAY_PORT)
    await site.start()
    log.info("[RELAY] HTTP on http://%s:%s", config.RELAY_HOST, config.RELAY_PORT)
    return runner


async def main():
    if not config.USER_TOKEN:
        log.error("GOLIVE_TOKEN not set. See .env.example.")
        sys.exit(1)
    relay_runner = await _start_relay()
    try:
        await client.start(config.USER_TOKEN)
    finally:
        if relay_runner is not None:
            try:
                await relay_runner.cleanup()
            except Exception:
                log.warning("[MAIN] relay cleanup failed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
