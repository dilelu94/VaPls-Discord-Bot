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
from streamer import VideoStream
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

_active_streams: dict[int, VideoStream] = {}


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
    log.info("[STREAM] guild=%s channel=%s url=%s", guild_id, channel_id, url[:120])

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

    log.info("[STREAM] establishing GoLive connection...")
    stream_conn = GoLiveConnection(client, guild_id, channel_id, vc)
    try:
        await stream_conn.connect()
    except Exception as e:
        log.exception("[STREAM] GoLive connection failed")
        return web.json_response({"error": f"GoLive connection failed: {e}"}, status=500)

    log.info("[STREAM] creating VideoStream url=%s", url[:80])
    stream = VideoStream(
        url=url,
        guild_id=guild_id,
        conn=stream_conn,
    )
    try:
        await stream.start()
        log.info("[STREAM] stream.start() OK")
    except Exception as e:
        log.exception("[STREAM] stream start failed")
        return web.json_response({"error": str(e)}, status=500)

    _active_streams[guild_id] = stream
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
