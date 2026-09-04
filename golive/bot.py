from __future__ import annotations

"""GoLive userbot: IPTV streaming via a dedicated Discord user account.

Runs separately from the indio userbot. No voice receive, no Whisper,
no VOSK, no DAVE — just FFmpeg → H.264 → RTP out a Discord UDP socket.

Endpoints:
  POST /stream         — start a Go Live stream in a voice channel
  POST /stopstream     — stop the active stream
  POST /stream/control — pause/resume/seek the active stream
"""

import asyncio
import json
import logging
import os
import time
import sys
from typing import Any, Optional

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
    from slopsoil.golive import GoLiveConnection



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
    def __init__(self, bot, guild_id, channel_id, vc, url, start_sec: float = 0.0, audio_track: int = 0, subtitle_track: int = -1):
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.vc = vc
        self.url = url
        self.start_sec = start_sec
        self.audio_track = audio_track
        self.subtitle_track = subtitle_track
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

    @staticmethod
    async def _resolve_stream_url(url: str) -> "tuple[str, str, bool]":
        """Resolve *url* to a (target_url, title, is_live) triple.

        For direct media files (detected by extension or Content-Type HEAD
        sniff) yt-dlp is skipped entirely.  Any HTTP-level yt-dlp failure
        falls back to using the raw URL so CDN links never surface as 500s.
        """
        target_url = url
        title = "Stream"
        is_live = True

        if not url.startswith(("http://", "https://")):
            return target_url, title, is_live

        from urllib.parse import urlparse
        import aiohttp
        from ytdlp import _yt_extract_url

        _path = urlparse(url).path.lower()
        _DIRECT_MEDIA_EXTS = (
            ".m3u8", ".mpd", ".m3u",
            ".mkv", ".mp4", ".webm", ".avi", ".mov",
            ".ts", ".flv", ".wmv", ".ogv", ".ogg",
        )
        _DIRECT_CONTENT_TYPES = ("video/", "audio/", "application/octet-stream")
        _FALLBACK_SIGNALS = (
            "Requested format is not available",
            "Unable to extract",
            "Unable to download webpage",
            "HTTP Error 400",
            "HTTP Error 403",
            "HTTP Error 404",
        )

        is_direct = (
            any(_path.endswith(ext) for ext in _DIRECT_MEDIA_EXTS)
            or ".m3u8" in url.lower()
        )

        # For extensionless URLs (e.g. CDN /dld/<uuid>?token=...) sniff
        # Content-Type via HEAD before deciding to invoke yt-dlp.
        if not is_direct:
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.head(
                        url,
                        allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=8),
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as resp:
                        ct = resp.headers.get("Content-Type", "").lower()
                        if any(ct.startswith(t) for t in _DIRECT_CONTENT_TYPES):
                            log.info(
                                "[STREAM] Content-Type=%r → direct media, skipping yt-dlp", ct
                            )
                            is_direct = True
                        else:
                            log.info("[STREAM] Content-Type=%r → will try yt-dlp", ct)
            except Exception as he:
                log.warning("[STREAM] HEAD request failed (%s), proceeding with yt-dlp", he)

        if is_direct:
            log.info("[STREAM] Using URL directly (no yt-dlp): %s", _path or url)
            return target_url, title, False  # is_live=False for files

        log.info("[STREAM] Checking stream URL via yt-dlp for %s", url)
        try:
            res = await _yt_extract_url(url)
        except Exception as e:
            err_str = str(e)
            if any(s in err_str for s in _FALLBACK_SIGNALS):
                log.warning(
                    "[STREAM] yt-dlp can't handle URL, falling back to direct: %s", e
                )
                return target_url, title, False
            log.warning("[STREAM] yt-dlp extraction failed: %s", e)
            raise RuntimeError(f"Failed to extract stream URL via yt-dlp: {e}")

        if res:
            target_url, title, is_live = res
            log.info(
                "[STREAM] Extracted stream: %s -> %s (live=%s)", title, target_url, is_live
            )
            return target_url, title, is_live

        raise RuntimeError("Failed to extract stream URL via yt-dlp")

    async def start(self):
        
        target_url, title, is_live = await self._resolve_stream_url(self.url)
        self.target_url = target_url
        self.title = title
        self.is_live = is_live

        log.info("[STREAM] Establishing GoLive connection...")
        
        async def _dummy_send(msg, **kwargs):
            log.info("[STREAM_STATUS] %s", msg)

        guild = client.get_guild(self.guild_id)
        channel = guild.get_channel(self.channel_id) if guild else None
        if guild and not self.vc:
            self.vc = _vc_for_guild(guild)

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
            start_time=self.start_sec,
            audio_track=self.audio_track,
            subtitle_track=self.subtitle_track,
        )
        self.conn = getattr(client, "live_connections", {}).get(self.guild_id)
        self.video_player = getattr(client, "video_players", {}).get(self.guild_id)
        self.video_ssrc = (self.conn.ssrc + 1) if (self.conn and hasattr(self.conn, "ssrc")) else 101

        self._inactivity_task = asyncio.create_task(self._inactivity_loop())

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
        url = self.queue[0]
        if url in self._prefetch_cache or url in self._prefetch_urls:
            return
        self._prefetch_urls.add(url)
        try:
            res = await self._resolve_stream_url(url)
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
                            
                            # Resolve target URL (prefetched while the
                            # previous reel was playing; inline fallback otherwise).
                            res = self._prefetch_cache.pop(next_url, None)
                            if res is None:
                                try:
                                    res = await self._resolve_stream_url(next_url)
                                except Exception as e:
                                    log.error("[STREAM] URL resolution failed for next URL: %s", e)
                                    continue
                            if res:
                                self.target_url, self.title, self.is_live = res
                                self.url = next_url
                            else:
                                log.error("[STREAM] Failed to resolve next URL")
                                continue
                            
                            # Set nickname to reflect new video
                            guild = client.get_guild(self.guild_id)
                            if guild:
                                _save_original_nickname(guild)
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

    @property
    def current_position(self) -> float:
        if self.video_player and hasattr(self.video_player, "current_position"):
            return self.video_player.current_position
        return 0.0

    def seek(self, target_sec: float):
        if self.video_player:
            self.video_player.seek(target_sec)

    async def stop(self, disconnect_voice: bool = True):
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
            await _restore_nickname(guild)

        log.info("[STREAM] Stream stopped and cleaned up")






_active_streams: dict[int, GoLiveStream] = {}

_nick_restore_tasks: dict[int, asyncio.Task] = {}
_original_nicknames: dict[int, Optional[str]] = {}

DEFAULT_NICKNAME: Optional[str] = None


def _save_original_nickname(guild: discord.Guild) -> None:
    """Save the bot's current nickname in guild before changing it for a stream."""
    if guild.id not in _original_nicknames:
        me = getattr(guild, "me", None) or guild.get_member(client.user.id)
        current_nick = me.nick if me else None
        _original_nicknames[guild.id] = current_nick
        log.info("[NICK] Saved original nick '%s' for guild=%s", current_nick, guild.id)


async def _set_nickname(guild: discord.Guild, name: Optional[str]) -> None:
    """Change the golive bot's nickname in a guild (32-char Discord limit). If name is None, resets nick to normal."""
    if os.getenv("ENABLE_NICKNAME_CHANGE", "true").lower() not in ("1", "true", "yes"):
        log.info("[NICK] skipped because ENABLE_NICKNAME_CHANGE=false")
        return
    nick = name[:32] if name else None
    try:
        me = getattr(guild, "me", None) or guild.get_member(client.user.id)
        if me is None:
            me = await guild.fetch_member(client.user.id)
        if me is not None:
            if me.nick != nick:
                await me.edit(nick=nick)
                log.info("[NICK] set to '%s' in guild=%s", nick, guild.id)
        else:
            log.warning("[NICK] bot member not found in guild=%s", guild.id)
    except Exception as e:
        log.warning("[NICK] failed to set nick in guild=%s: %s", guild.id, e)


async def _restore_nickname(guild: discord.Guild) -> None:
    """Immediately restore the bot's original nickname in a guild."""
    task = _nick_restore_tasks.pop(guild.id, None)
    if task and not task.done():
        task.cancel()

    target_nick = _original_nicknames.pop(guild.id, DEFAULT_NICKNAME)
    log.info("[NICK] Restoring original nick '%s' in guild=%s", target_nick, guild.id)
    await _set_nickname(guild, target_nick)


def _schedule_nickname_restore(guild: discord.Guild) -> None:
    """Schedule immediate nickname restoration task."""
    task = _nick_restore_tasks.get(guild.id)
    if task and not task.done():
        task.cancel()
    _nick_restore_tasks[guild.id] = asyncio.create_task(_restore_nickname(guild))


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
        start_sec = float(data.get("start_sec", 0.0))
        audio_track = int(data.get("audio_track", 0))
        subtitle_track = int(data.get("subtitle_track", -1))
    except Exception as e:
        log.warning("[STREAM] invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)
    if not url:
        log.warning("[STREAM] empty url")
        return web.json_response({"error": "empty url"}, status=400)
    stream_title = str(data.get("channel_name", "")).strip() or "Stream"
    log.info(
        "[STREAM] guild=%s channel=%s url=%s title=%s start_sec=%.1f audio_track=%d subtitle_track=%d",
        guild_id,
        channel_id,
        url[:120],
        stream_title,
        start_sec,
        audio_track,
        subtitle_track,
    )

    if not client.is_ready():
        return web.json_response({"error": "client not ready"}, status=503)

    existing = _active_streams.get(guild_id)
    if existing and not getattr(existing, "_stopped", True):
        if hasattr(existing, "queue"):
            existing.queue.append(url)
            existing.queue_titles.append(stream_title)
            log.info("[STREAM] Queued video for guild=%s: %s (pos %d)", guild_id, url, len(existing.queue))
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
        _save_original_nickname(guild)
        task = _nick_restore_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
        await _set_nickname(guild, f"GoLive - {stream_title}")

    stream = GoLiveStream(client, guild_id, channel_id, vc, url, start_sec=start_sec, audio_track=audio_track, subtitle_track=subtitle_track)
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

    log.info("[STOPSTREAM] received stop request for guild=%s", guild_id)

    # 1. Disconnect GoLiveConnection first so screenshare (op 19 STREAM_DELETE) is sent to Discord
    conn = getattr(client, "live_connections", {}).pop(guild_id, None)
    if conn:
        try:
            log.info("[STOPSTREAM] disconnecting GoLiveConnection for guild=%s", guild_id)
            await asyncio.wait_for(conn.disconnect(), timeout=3.0)
        except Exception as e:
            log.warning("[STOPSTREAM] conn disconnect error: %s", e)

    # 2. Stop video player thread if active
    vp = getattr(client, "video_players", {}).pop(guild_id, None)
    if vp:
        try:
            vp.stop()
        except Exception:
            pass

    # 3. Cancel slopsoil background tasks
    try:
        slopsoil_cancel_live_stream(client, guild_id)
    except Exception as e:
        log.warning("[STOPSTREAM] slopsoil_cancel_live_stream error: %s", e)

    # 4. Stop GoLiveStream object without inline voice disconnect
    stream = _active_streams.pop(guild_id, None)
    if stream:
        try:
            await stream.stop(disconnect_voice=False)
        except Exception as e:
            log.warning("[STOPSTREAM] stream stop failed: %s", e)


    # 5. Schedule voice client disconnect in background after sending HTTP response
    async def _delayed_vc_disconnect():
        await asyncio.sleep(0.2)
        guild = client.get_guild(guild_id)
        if guild:
            await _restore_nickname(guild)
            vc = _vc_for_guild(guild)
            if vc and vc.is_connected():
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    log.warning("[STOPSTREAM] vc disconnect error: %s", e)

    asyncio.create_task(_delayed_vc_disconnect())
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
    elif action == "resume_pos":
        curr_pos = 0.0
        player = getattr(stream, "video_player", None) or vp
        if player and hasattr(player, "current_position"):
            curr_pos = player.current_position
        resume_sec = max(0.0, curr_pos - 2.0)
        if stream and hasattr(stream, "seek"):
            stream.seek(resume_sec)
        elif vp and hasattr(vp, "seek"):
            vp.seek(resume_sec)
        return web.json_response(
            {"status": "resumed_from_pos", "pos": resume_sec, "guild_id": guild_id}
        )
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


def _has_active_stream(guild_id: int) -> bool:
    """Return True if an active video/live stream is currently running for guild_id."""
    if hasattr(client, "stream_tasks") and guild_id in client.stream_tasks:
        task = client.stream_tasks[guild_id]
        if not task.done():
            return True
    if hasattr(client, "live_connections") and guild_id in client.live_connections:
        return True
    if hasattr(client, "video_players") and guild_id in client.video_players:
        return True
    stream = _active_streams.get(guild_id)
    if stream is not None and not getattr(stream, "_stopped", True):
        return True
    return False


async def _idle_watcher(guild_id: int):
    log.info("[WATCHDOG] Started idle watchdog for guild=%s", guild_id)
    idle_since = time.monotonic()
    try:
        while True:
            await asyncio.sleep(2)

            # Check if there is an active stream in this guild
            has_stream = _has_active_stream(guild_id)

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
                        await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
                    except Exception as e:
                        log.warning("[WATCHDOG] Disconnect failed: %s", e)
                    guild = client.get_guild(guild_id)
                    if guild:
                        await _restore_nickname(guild)
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
    for guild in client.guilds:
        if guild.id not in _active_streams:
            me = getattr(guild, "me", None) or guild.get_member(client.user.id)
            if me and me.nick and me.nick.startswith("GoLive - "):
                log.info("[NICK] Clearing stale stream nick '%s' in guild=%s", me.nick, guild.id)
                await _set_nickname(guild, None)


@client.event
async def on_voice_state_update(member, before, after):
    if member.id == client.user.id:
        if after.channel is not None:
            # Joined voice
            _start_idle_watchdog(after.channel.guild.id)
        else:
            # Left voice
            if before.channel is not None:
                guild = before.channel.guild
                _stop_idle_watchdog(guild.id)
                await _restore_nickname(guild)


# ---------- Main ------------------------------------------------------------


try:
    from slopsoil.engine import start_live_stream as slopsoil_start_live_stream, cancel_live_stream as slopsoil_cancel_live_stream
except ModuleNotFoundError:
    from golive.slopsoil.engine import start_live_stream as slopsoil_start_live_stream, cancel_live_stream as slopsoil_cancel_live_stream


async def _relay_watchstream_snapshot(request: web.Request) -> web.Response:
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])
        target_user_id = int(data["target_user_id"])
        duration = float(data.get("duration", 3.0))
    except Exception as e:
        log.warning("[WATCHSTREAM] invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)

    if not client.is_ready():
        return web.json_response({"error": "client not ready"}, status=503)

    guild = client.get_guild(guild_id)
    if not guild:
        return web.json_response({"error": "guild not found"}, status=404)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        return web.json_response({"error": "not a voice channel"}, status=400)

    vc = _vc_for_guild(guild)
    if not vc or not vc.is_connected():
        await _join_channel(channel)
        vc = _vc_for_guild(guild)
    if not vc:
        return web.json_response({"error": "could not join channel"}, status=500)

    try:
        from golive.golive_watcher import GoLiveWatcherConnection
    except ModuleNotFoundError:
        from golive_watcher import GoLiveWatcherConnection

    watcher = GoLiveWatcherConnection(
        bot=client,
        guild_id=guild_id,
        channel_id=channel_id,
        target_user_id=target_user_id,
        vc=vc,
    )

    try:
        jpg_path = await watcher.capture_snapshot(duration_sec=duration)
        await watcher.disconnect()
        if jpg_path and os.path.exists(jpg_path):
            return web.json_response({
                "success": True,
                "snapshot_path": jpg_path,
                "guild_id": guild_id,
                "target_user_id": target_user_id,
            })
        else:
            buf_len = len(watcher.receiver._raw_nal_buffer) if hasattr(watcher, "receiver") else 0
            if buf_len == 0:
                reason = "No se recibieron paquetes de video (asegurate de estar transmitiendo pantalla en Go Live)"
            else:
                reason = "Error al decodificar cuadro de video del stream"
            return web.json_response({"success": False, "error": reason}, status=500)
    except Exception as e:
        log.exception("[WATCHSTREAM] snapshot failed")
        await watcher.disconnect()
        return web.json_response({"error": str(e)}, status=500)


async def _relay_watchstream_stop(request: web.Request) -> web.Response:
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])
        target_user_id = int(data["target_user_id"])
    except Exception as e:
        return web.json_response({"error": "invalid body"}, status=400)

    guild = client.get_guild(guild_id)
    vc = _vc_for_guild(guild) if guild else None
    if vc:
        try:
            from golive.golive_watcher import GoLiveWatcherConnection
        except ModuleNotFoundError:
            from golive_watcher import GoLiveWatcherConnection
        watcher = GoLiveWatcherConnection(client, guild_id, channel_id, target_user_id, vc)
        await watcher.disconnect()

    return web.json_response({"stopped": True, "guild_id": guild_id})


async def _start_relay() -> Optional[web.AppRunner]:
    if not config.RELAY_SECRET:
        log.warning("RELAY_SECRET not set — HTTP relay disabled.")
        return None
    app = web.Application()
    app.router.add_post("/stream", _relay_stream)
    app.router.add_post("/stopstream", _relay_stopstream)
    app.router.add_post("/stream/control", _relay_stream_control)
    app.router.add_post("/watchstream/snapshot", _relay_watchstream_snapshot)
    app.router.add_post("/watchstream/stop", _relay_watchstream_stop)
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
