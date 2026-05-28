"""HTTP API for the Telegram bridge bot.

Runs an aiohttp server in the same asyncio loop as py-cord.
Auth: every request must carry header X-API-Secret matching config.API_SECRET.
Binds to config.API_HOST:config.API_PORT (default 127.0.0.1:8080).
"""
from __future__ import annotations

import asyncio
import os
import uuid
import logging
from typing import Optional

import discord
from aiohttp import web

import config
from playCommand import guildPlayers

logger = logging.getLogger("apiServer")


def _check_auth(request: web.Request) -> Optional[web.Response]:
    if not config.API_SECRET:
        return web.json_response({"error": "API_SECRET not configured"}, status=503)
    if request.headers.get("X-API-Secret") != config.API_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    err = _check_auth(request)
    if err is not None:
        return err
    return await handler(request)


def _serialize_member_voice(member: discord.Member) -> dict:
    vs = member.voice
    return {
        "id": member.id,
        "display_name": member.display_name,
        "name": member.name,
        "is_bot": member.bot,
        "muted": bool(vs and vs.mute),
        "deafened": bool(vs and vs.deaf),
        "self_mute": bool(vs and vs.self_mute),
        "self_deaf": bool(vs and vs.self_deaf),
    }


def _resolve_guild(bot: discord.Bot, guild_id: int) -> Optional[discord.Guild]:
    return bot.get_guild(guild_id)


def make_app(bot: discord.Bot) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=25 * 1024 * 1024)

    async def status(_: web.Request) -> web.Response:
        return web.json_response({
            "ready": bot.is_ready(),
            "guilds": len(bot.guilds),
            "voice_clients": [
                {
                    "guild_id": vc.guild.id,
                    "channel_id": vc.channel.id if vc.channel else None,
                    "channel_name": vc.channel.name if vc.channel else None,
                    "playing": vc.is_playing(),
                }
                for vc in bot.voice_clients
            ],
        })

    async def members(request: web.Request) -> web.Response:
        try:
            guild_id = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "missing or invalid guild_id"}, status=400)
        voice_only = request.query.get("voice_only", "true").lower() == "true"

        guild = _resolve_guild(bot, guild_id)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        voice_channels = [
            {
                "id": ch.id,
                "name": ch.name,
                "members": [_serialize_member_voice(m) for m in ch.members],
            }
            for ch in guild.voice_channels
        ]
        payload = {"voice_channels": voice_channels}
        if not voice_only:
            payload["guild_members"] = [
                {
                    "id": m.id,
                    "display_name": m.display_name,
                    "name": m.name,
                    "is_bot": m.bot,
                    "status": str(getattr(m, "status", "unknown")),
                }
                for m in guild.members
            ]
        return web.json_response(payload)

    async def user(request: web.Request) -> web.Response:
        try:
            user_id = int(request.match_info["user_id"])
            guild_id = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid params"}, status=400)

        guild = _resolve_guild(bot, guild_id)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return web.json_response({"error": "user not found"}, status=404)

        vs = member.voice
        voice = None
        if vs and vs.channel:
            voice = {
                "channel_id": vs.channel.id,
                "channel_name": vs.channel.name,
                "self_mute": vs.self_mute,
                "self_deaf": vs.self_deaf,
            }
        activity = None
        acts = getattr(member, "activities", None) or []
        if acts:
            activity = str(acts[0].name) if hasattr(acts[0], "name") else str(acts[0])

        return web.json_response({
            "id": member.id,
            "display_name": member.display_name,
            "name": member.name,
            "status": str(getattr(member, "status", "unknown")),
            "activity": activity,
            "voice": voice,
            "roles": [r.name for r in member.roles],
        })

    async def send_message(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            guild_id = int(data["guild_id"])
            channel_id = int(data["channel_id"])
            content = str(data["content"])
        except Exception:
            return web.json_response({"error": "invalid body"}, status=400)
        sender_label = str(data.get("sender_label") or "TG")

        guild = _resolve_guild(bot, guild_id)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return web.json_response({"error": "channel not found"}, status=404)

        try:
            msg = await channel.send(f"**[TG/{sender_label}]** {content}")
        except Exception as e:
            logger.exception("send_message failed")
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"message_id": msg.id})

    async def _pick_auto_voice_channel(guild: discord.Guild) -> Optional[discord.VoiceChannel]:
        candidates = [
            (ch, sum(1 for m in ch.members if not m.bot))
            for ch in guild.voice_channels
        ]
        candidates = [c for c in candidates if c[1] > 0]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    async def play_audio(request: web.Request) -> web.Response:
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"error": "expected multipart"}, status=400)

        reader = await request.multipart()
        guild_id: Optional[int] = None
        target_channel_id: Optional[int] = None
        upload_path: Optional[str] = None
        filename: Optional[str] = None

        downloads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(downloads_dir, exist_ok=True)

        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "guild_id":
                guild_id = int((await part.read()).decode())
            elif part.name == "channel_id":
                raw = (await part.read()).decode().strip()
                if raw:
                    target_channel_id = int(raw)
            elif part.name == "file":
                ext = os.path.splitext(part.filename or "")[1] or ".ogg"
                filename = f"tg_{uuid.uuid4().hex}{ext}"
                upload_path = os.path.join(downloads_dir, filename)
                with open(upload_path, "wb") as f:
                    while chunk := await part.read_chunk():
                        f.write(chunk)

        if guild_id is None or upload_path is None:
            return web.json_response({"error": "missing guild_id or file"}, status=400)

        guild = _resolve_guild(bot, guild_id)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if vc is None or not vc.is_connected():
            channel = None
            if target_channel_id:
                ch = guild.get_channel(target_channel_id)
                if isinstance(ch, discord.VoiceChannel):
                    channel = ch
            if channel is None:
                channel = await _pick_auto_voice_channel(guild)
            if channel is None:
                try:
                    os.remove(upload_path)
                except Exception:
                    pass
                return web.json_response({"error": "no active voice channel and no users in any voice channel"}, status=409)
            try:
                vc = await channel.connect(reconnect=True, timeout=10.0)
            except Exception as e:
                try:
                    os.remove(upload_path)
                except Exception:
                    pass
                return web.json_response({"error": f"failed to join: {e}"}, status=500)

        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.2)
        except Exception:
            pass

        def _cleanup(_err):
            try:
                os.remove(upload_path)
            except Exception:
                pass

        try:
            vc.play(discord.FFmpegOpusAudio(upload_path), after=_cleanup)
        except Exception as e:
            _cleanup(None)
            return web.json_response({"error": f"play failed: {e}"}, status=500)

        return web.json_response({
            "played": True,
            "channel_id": vc.channel.id if vc.channel else None,
            "channel_name": vc.channel.name if vc.channel else None,
        })

    async def queue(request: web.Request) -> web.Response:
        try:
            guild_id = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "missing or invalid guild_id"}, status=400)
        gp = guildPlayers.get(guild_id)
        if gp is None:
            return web.json_response({
                "current": None,
                "queue": [],
                "history_count": 0,
                "is_paused": False,
                "is_playing": False,
            })
        vc = gp.vc
        return web.json_response({
            "current": gp.currentSong,
            "queue": list(gp.queue),
            "history_count": len(gp.history),
            "is_paused": bool(vc and vc.is_paused()),
            "is_playing": bool(vc and vc.is_playing()),
        })

    app.router.add_get("/status", status)
    app.router.add_get("/members", members)
    app.router.add_get("/user/{user_id}", user)
    app.router.add_post("/message", send_message)
    app.router.add_post("/play-audio", play_audio)
    app.router.add_get("/queue", queue)
    return app


async def start_api_server(bot: discord.Bot) -> web.AppRunner:
    if not config.API_SECRET:
        logger.warning("API_SECRET is empty - HTTP API will reject all requests. Set API_SECRET in .env to enable.")
    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.API_HOST, port=config.API_PORT)
    await site.start()
    logger.info(f"HTTP API listening on http://{config.API_HOST}:{config.API_PORT}")
    return runner
