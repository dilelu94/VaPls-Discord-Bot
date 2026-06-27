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
from golive_connection import GoLiveConnection
from instagram_feed import InstagramReelFeed
from instagram_streamer import InstagramGoLiveStream


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

    async def start(self):
        from ytdlp import _yt_extract_url
        
        target_url = self.url
        title = "Stream"
        is_live = True
        
        if target_url.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            _path = urlparse(target_url).path.lower()
            if _path.endswith((".m3u8", ".mpd", ".m3u")):
                log.info("[STREAM] Direct stream URL detected — skipping yt-dlp")
            else:
                log.info("[STREAM] Checking stream URL via yt-dlp for %s", target_url)
                try:
                    res = await _yt_extract_url(target_url)
                except Exception as e:
                    log.warning("[STREAM] yt-dlp extraction failed: %s", e)
                    res = None

                if res:
                    target_url, title, self.is_live = res
                    log.info("[STREAM] Extracted stream: %s -> %s (live=%s)", title, target_url, self.is_live)
                else:
                    raise RuntimeError("Failed to extract stream URL via yt-dlp")
        
        self.target_url = target_url
        self.title = title

        log.info("[STREAM] Establishing GoLive connection...")
        self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
        await self.conn.connect(timeout=30.0)
        self.video_ssrc = self.conn.ssrc + 1
        
        await self._start_players()
        self._inactivity_task = asyncio.create_task(self._inactivity_loop())

    async def _start_players(self):
        from streamer import H264VideoPlayer, _stream_fps
        from golive_connection import _GoLiveVCProxy, GoLiveAudioSender
        proxy_vc = _GoLiveVCProxy(self.conn)
        self.video_player = H264VideoPlayer(
            url=self.target_url,
            voice_client=proxy_vc,
            fps=_stream_fps(),
            live=self.is_live,
            audio=True,
        )
        self.video_player.start()
        log.info("[STREAM] Video player started for '%s'", self.title)
        
        log.info("[STREAM] Waiting for audio FIFO...")
        try:
            f = await asyncio.wait_for(
                asyncio.to_thread(open, self.video_player.audio_fifo, "rb"),
                timeout=15.0,
            )
        except TimeoutError:
            log.error("[STREAM] Timed out waiting for audio FIFO")
            raise RuntimeError("Timed out waiting for audio FIFO")
            
        self.audio_sender = GoLiveAudioSender(
            file_obj=f,
            conn=self.conn,
            is_source_active=self.video_player.is_source_active,
        )
        self.audio_sender.start()
        log.info("[STREAM] Audio sender started for '%s'", self.title)

    async def _stop_players(self):
        if self.video_player:
            self.video_player.stop()
        if self.audio_sender:
            self.audio_sender.stop()
        
        await asyncio.to_thread(self._wait_players)
        
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

    async def _inactivity_loop(self):
        """Monitors player health and reconnects if live, or auto-stops."""
        try:
            while not self._stopped:
                await asyncio.sleep(2)
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
                        log.info("[STREAM] Video player ended naturally — auto-stopping")
                        break
        except asyncio.CancelledError:
            return

        if not self._stopped:
            _active_streams.pop(self.guild_id, None)
            await self.stop()

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

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True

        if self._inactivity_task:
            self._inactivity_task.cancel()
            self._inactivity_task = None

        log.info("[STREAM] Stopping stream...")
        try:
            await self._stop_players()
        except Exception:
            pass
        # Drain window: let any in-flight nacl/opus encrypt calls complete
        # before closing the UDP socket, to avoid heap corruption from
        # concurrent C-level crypto operations.
        await asyncio.sleep(0.05)
        if self.conn:
            try:
                await self.conn.disconnect()
            except Exception:
                pass

        # Disconnect from voice channel
        if self.vc and self.vc.is_connected():
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass

        guild = client.get_guild(self.guild_id)
        if guild:
            _schedule_nickname_restore(guild)

        log.info("[STREAM] Stream stopped and cleaned up")


_active_streams: dict[int, GoLiveStream] = {}
_nick_restore_tasks: dict[int, asyncio.Task] = {}

DEFAULT_NICKNAME = "GoLive - VaPls"


async def _set_nickname(guild: discord.Guild, name: str) -> None:
    """Change the golive bot's nickname in a guild (32-char Discord limit)."""
    nick = name[:32]
    try:
        me = guild.get_member(client.user.id)
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
    stream_title = str(data.get("channel_name", "")).strip() or None
    log.info("[STREAM] guild=%s channel=%s url=%s title=%s", guild_id, channel_id, url[:120], stream_title)

    if not client.is_ready():
        log.info("[STREAM] client not ready, waiting up to 30s...")
        for _ in range(30):
            if client.is_ready():
                break
            await asyncio.sleep(1)
        if not client.is_ready():
            log.warning("[STREAM] client still not ready after 30s")
            return web.json_response({"error": "client not ready"}, status=503)

    existing = _active_streams.pop(guild_id, None)
    if existing:
        log.info("[STREAM] stopping existing stream for guild=%s", guild_id)
        await existing.stop()

    guild = client.get_guild(guild_id)
    if guild is None:
        log.warning("[STREAM] guild not found: %s", guild_id)
        return web.json_response({"error": "guild not found"}, status=404)
    log.info("[STREAM] guild=%s resolved", guild_id)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        try:
            channel = await client.fetch_channel(channel_id)
            log.info("[STREAM] channel fetched via API: %s", channel_id)
        except Exception as e:
            log.warning("[STREAM] channel fetch failed: %s", e)
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not isinstance(channel, discord.VoiceChannel):
        log.warning("[STREAM] channel=%s is not voice", channel_id)
        return web.json_response({"error": "not a voice channel"}, status=400)
    if not _guild_allowed(guild.id):
        log.warning("[STREAM] guild=%s not allowed", guild_id)
        return web.json_response({"error": "guild not allowed"}, status=403)

    log.info("[STREAM] joining channel=%s", channel_id)
    try:
        await _join_channel(channel)
        log.info("[STREAM] join OK")
    except Exception as e:
        log.exception("[STREAM] join failed")
        return web.json_response({"error": f"join failed: {e}"}, status=500)

    vc = _vc_for_guild(guild)
    if vc is None or not vc.is_connected():
        log.warning("[STREAM] not connected after join (vc=%s)", vc)
        return web.json_response({"error": "not connected"}, status=500)
    log.info("[STREAM] vc=%s", type(vc).__name__)

    log.info("[STREAM] creating GoLiveStream url=%s", url[:80])
    stream = GoLiveStream(client, guild_id, channel_id, vc, url)
    try:
        await stream.start()
        log.info("[STREAM] stream start OK")
    except Exception as e:
        log.exception("[STREAM] stream start failed")
        await stream.stop()
        return web.json_response({"error": str(e)}, status=500)

    _active_streams[guild_id] = stream
    if stream_title:
        task = _nick_restore_tasks.pop(guild_id, None)
        if task:
            task.cancel()
        await _set_nickname(guild, f"GoLive - {stream_title}")
    log.info("[STREAM] started guild=%s channel=%s", guild_id, channel.name)
    return web.json_response(
        {
            "started": True,
            "guild_id": guild_id,
            "channel_name": channel.name,
            "video_ssrc": stream.video_ssrc,
            "is_live": stream.is_live,
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

    stream = _active_streams.pop(guild_id, None)
    if stream is None:
        log.info("[STOPSTREAM] no active stream for guild=%s", guild_id)
        return web.json_response({"error": "no active stream"}, status=404)

    try:
        await stream.stop()
    except Exception as e:
        log.exception("[STOPSTREAM] stop failed")
        return web.json_response({"error": str(e)}, status=500)

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
    if stream is None:
        return web.json_response({"error": "no active stream"}, status=404)

    if action == "pause":
        stream.pause()
    elif action == "resume":
        stream.resume()
    elif action == "seek":
        try:
            target_sec = float(data.get("timestamp", 0))
            stream.seek(target_sec)
        except ValueError:
            return web.json_response({"error": "invalid timestamp"}, status=400)
    elif action == "status":
        pass  # Just returns success below if stream exists
    else:
        return web.json_response({"error": "invalid action"}, status=400)

    return web.json_response({"success": True})


# ---------- Instagram relay --------------------------------------------------

_instagram_feed: InstagramReelFeed | None = None


async def _relay_instagram(request: web.Request) -> web.Response:
    log.info("[INSTAGRAM] request from %s", request.remote)
    if not config.RELAY_SECRET:
        return web.json_response({"error": "relay disabled"}, status=503)
    if request.headers.get("X-API-Secret") != config.RELAY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
        guild_id = int(data["guild_id"])
        channel_id = int(data["channel_id"])
        reel_url = str(data.get("url", "")).strip() or None
    except Exception as e:
        log.warning("[INSTAGRAM] invalid body: %s", e)
        return web.json_response({"error": "invalid body"}, status=400)

    if not client.is_ready():
        log.info("[INSTAGRAM] client not ready, waiting up to 30s...")
        for _ in range(30):
            if client.is_ready():
                break
            await asyncio.sleep(1)
        if not client.is_ready():
            log.warning("[INSTAGRAM] client still not ready after 30s")
            return web.json_response({"error": "client not ready"}, status=503)

    existing = _active_streams.pop(guild_id, None)
    if existing:
        log.info("[INSTAGRAM] stopping existing stream for guild=%s", guild_id)
        await existing.stop()

    guild = client.get_guild(guild_id)
    if guild is None:
        log.warning("[INSTAGRAM] guild not found: %s", guild_id)
        return web.json_response({"error": "guild not found"}, status=404)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as e:
            log.warning("[INSTAGRAM] channel fetch failed: %s", e)
            return web.json_response({"error": f"channel not found: {e}"}, status=404)
    if not isinstance(channel, discord.VoiceChannel):
        log.warning("[INSTAGRAM] channel=%s is not voice", channel_id)
        return web.json_response({"error": "not a voice channel"}, status=400)
    if not _guild_allowed(guild.id):
        log.warning("[INSTAGRAM] guild=%s not allowed", guild_id)
        return web.json_response({"error": "guild not allowed"}, status=403)

    log.info("[INSTAGRAM] joining channel=%s", channel_id)
    try:
        await _join_channel(channel)
    except Exception as e:
        log.exception("[INSTAGRAM] join failed")
        return web.json_response({"error": f"join failed: {e}"}, status=500)

    vc = _vc_for_guild(guild)
    if vc is None or not vc.is_connected():
        log.warning("[INSTAGRAM] not connected after join (vc=%s)", vc)
        return web.json_response({"error": "not connected"}, status=500)

    # ── URL mode (yt-dlp, no credentials) vs feed mode (yt-dlp flat_playlist) ──────
    if reel_url:
        from ytdlp import _yt_extract_instagram

        log.info("[INSTAGRAM] Extracting reel URL via yt-dlp: %s", reel_url[:80])
        extracted = await _yt_extract_instagram(reel_url)
        if not extracted:
            log.warning("[INSTAGRAM] yt-dlp extraction failed — disconnecting")
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            return web.json_response(
                {"error": "failed to extract reel URL via yt-dlp"}, status=500
            )
        log.info(
            "[INSTAGRAM] Extracted: %s (video=%s.. audio=%s)",
            extracted.get("title", "?"),
            (extracted.get("video_url") or "")[:60],
            "yes" if extracted.get("audio_url") else "no",
        )
        stream = InstagramGoLiveStream(
            client, guild_id, channel_id, vc, reel_url=extracted
        )
    else:
        # Feed mode — yt-dlp flat_playlist, no credentials needed
        global _instagram_feed
        if _instagram_feed is None:
            source = os.environ.get(
                "INSTAGRAM_REEL_SOURCE",
                "https://www.instagram.com/explore/tags/reels/",
            )
            _instagram_feed = InstagramReelFeed(source_url=source)
            log.info("[INSTAGRAM] Feed mode source: %s", source)

        stream = InstagramGoLiveStream(
            client, guild_id, channel_id, vc, feed=_instagram_feed
        )

    log.info("[INSTAGRAM] creating InstagramGoLiveStream")
    try:
        await stream.start()
    except Exception as e:
        log.exception("[INSTAGRAM] stream start failed")
        await stream.stop()
        return web.json_response({"error": str(e)}, status=500)

    _active_streams[guild_id] = stream
    log.info("[INSTAGRAM] started guild=%s channel=%s", guild_id, channel.name)
    return web.json_response(
        {
            "started": True,
            "guild_id": guild_id,
            "channel_name": channel.name,
            "video_ssrc": stream.video_ssrc,
        }
    )


# ---------- Events ----------------------------------------------------------


@client.event
async def on_ready():
    log.info("GoLive online as %s (id=%s)", client.user, client.user.id)


# ---------- Main ------------------------------------------------------------


async def _start_relay() -> Optional[web.AppRunner]:
    if not config.RELAY_SECRET:
        log.warning("RELAY_SECRET not set — HTTP relay disabled.")
        return None
    app = web.Application()
    app.router.add_post("/stream", _relay_stream)
    app.router.add_post("/stopstream", _relay_stopstream)
    app.router.add_post("/stream/control", _relay_stream_control)
    app.router.add_post("/instagram", _relay_instagram)
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
