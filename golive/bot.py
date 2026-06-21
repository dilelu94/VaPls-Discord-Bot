"""GoLive userbot: IPTV streaming via a dedicated Discord user account.

Runs separately from the indio userbot. No voice receive, no Whisper,
no VOSK, no DAVE — just FFmpeg → H.264 → RTP out a Discord UDP socket.

Endpoints:
  POST /stream     — start an IPTV Go Live in a voice channel
  POST /stopstream — stop the active stream
"""

import asyncio
import json
import logging
import os
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
        self.tmp_dir = None
        self.video_ssrc = None
        self._stopped = False
        self._inactivity_task = None

    async def start(self):
        import tempfile
        from ytdlp import _yt_extract_live_url, _yt_download, _yt_remove_dir
        
        target_url = self.url
        title = "Stream"
        is_live = True
        
        if target_url.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            _path = urlparse(target_url).path.lower()
            if _path.endswith((".m3u8", ".mpd", ".m3u")):
                log.info("[STREAM] Direct stream URL detected — skipping yt-dlp")
            else:
                log.info("[STREAM] Checking live status via yt-dlp for %s", target_url)
                try:
                    live_res = await _yt_extract_live_url(target_url)
                except Exception as e:
                    log.warning("[STREAM] live check failed: %s", e)
                    live_res = None

                if live_res:
                    target_url, title = live_res
                    log.info("[STREAM] Live stream detected: %s -> %s", title, target_url)
                else:
                    log.info("[STREAM] VOD stream detected. Downloading via yt-dlp...")
                    self.tmp_dir = tempfile.mkdtemp(prefix="golive_yt_")
                    try:
                        file_path, title = await _yt_download(self.url, self.tmp_dir)
                        target_url = file_path
                        is_live = False
                        log.info("[STREAM] Download complete: %s -> %s", title, target_url)
                    except Exception as e:
                        log.exception("[STREAM] Download failed")
                        if self.tmp_dir:
                            _yt_remove_dir(self.tmp_dir)
                            self.tmp_dir = None
                        raise RuntimeError(f"Download failed: {e}")
        
        log.info("[STREAM] Establishing GoLive connection...")
        self.conn = GoLiveConnection(self.bot, self.guild_id, self.channel_id, self.vc)
        await self.conn.connect(timeout=30.0)
        self.video_ssrc = self.conn.ssrc + 1
        
        from streamer import H264VideoPlayer, _stream_fps
        from golive_connection import _GoLiveVCProxy, GoLiveAudioSender
        proxy_vc = _GoLiveVCProxy(self.conn)
        self.video_player = H264VideoPlayer(
            url=target_url,
            voice_client=proxy_vc,
            fps=_stream_fps(),
            live=is_live,
            audio=True,
        )
        self.video_player.start()
        log.info("[STREAM] Video player started for '%s'", title)
        
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
        log.info("[STREAM] Audio sender started for '%s'", title)

        self._inactivity_task = asyncio.create_task(self._inactivity_loop())

    async def _inactivity_loop(self):
        """Every 30s: auto-stop on natural end only."""
        try:
            while not self._stopped:
                await asyncio.sleep(30)
                if self._stopped:
                    break

                if self.video_player and not self.video_player.is_alive():
                    log.info("[STREAM] Video player ended naturally — auto-stopping")
                    break
        except asyncio.CancelledError:
            return

        if not self._stopped:
            _active_streams.pop(self.guild_id, None)
            await self.stop()

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True

        if self._inactivity_task:
            self._inactivity_task.cancel()
            self._inactivity_task = None

        log.info("[STREAM] Stopping stream...")
        from ytdlp import _yt_remove_dir
        if self.video_player:
            try:
                self.video_player.stop()
            except Exception:
                pass
        if self.audio_sender and self.audio_sender.is_alive():
            try:
                self.audio_sender.stop()
            except Exception:
                pass
        if self.conn:
            try:
                await self.conn.disconnect()
            except Exception:
                pass
        if self.tmp_dir:
            try:
                _yt_remove_dir(self.tmp_dir)
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
        log.warning("[STREAM] client not ready")
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
