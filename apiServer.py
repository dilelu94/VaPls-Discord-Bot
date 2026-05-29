"""HTTP API served alongside the main Discord bot.

Runs an aiohttp server in the same asyncio loop as py-cord. Every request must
include header X-API-Secret matching config.API_SECRET. Binds to
config.API_HOST:config.API_PORT (default 127.0.0.1:8080).
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


def _checkAuth(request: web.Request) -> Optional[web.Response]:
    """Validate the X-API-Secret header for a request.

    Args:
        request: Incoming aiohttp request.

    Returns:
        None if authorized, otherwise a JSON error response.
    """
    if not config.API_SECRET:
        return web.json_response({"error": "API_SECRET not configured"}, status=503)
    if request.headers.get("X-API-Secret") != config.API_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


@web.middleware
async def authMiddleware(request: web.Request, handler):
    """Reject unauthorized requests before hitting handlers.

    Args:
        request: Incoming aiohttp request.
        handler: Downstream request handler.

    Returns:
        A web.Response from the handler or an auth error response.

    Async:
        This function is a coroutine and must be awaited by aiohttp.
    """
    err = _checkAuth(request)
    if err is not None:
        return err
    return await handler(request)


def _serializeMemberVoice(member: discord.Member) -> dict:
    """Serialize a member's voice state into a JSON-ready dict.

    Args:
        member: Discord member.

    Returns:
        Dictionary with voice-related fields.
    """
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


def _resolveGuild(bot: discord.Bot, guildId: int) -> Optional[discord.Guild]:
    """Resolve a guild from the bot cache.

    Args:
        bot: Discord bot client.
        guildId: Guild ID to resolve.

    Returns:
        Guild instance if cached; otherwise None.
    """
    return bot.get_guild(guildId)


def makeApp(bot: discord.Bot) -> web.Application:
    """Create the aiohttp application with all API routes.

    Args:
        bot: Discord bot instance.

    Returns:
        Configured aiohttp Application.
    """
    app = web.Application(middlewares=[authMiddleware], client_max_size=25 * 1024 * 1024)

    async def status(_: web.Request) -> web.Response:
        """Return the bot readiness and voice client status.

        Returns:
            JSON response with readiness, guild count, and voice clients.

        Async:
            This function is a coroutine and must be awaited.
        """
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
        """List voice channels and optionally full guild members.

        Args:
            request: Incoming HTTP request with guild_id and voice_only.

        Returns:
            JSON response containing voice channel memberships.

        Async:
            This function is a coroutine and must be awaited.
        """
        try:
            guildId = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "missing or invalid guild_id"}, status=400)
        voiceOnly = request.query.get("voice_only", "true").lower() == "true"

        guild = _resolveGuild(bot, guildId)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        voiceChannels = [
            {
                "id": ch.id,
                "name": ch.name,
                "members": [_serializeMemberVoice(m) for m in ch.members],
            }
            for ch in guild.voice_channels
        ]
        payload = {"voice_channels": voiceChannels}
        if not voiceOnly:
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
        """Return details for a single guild member.

        Args:
            request: Incoming HTTP request with user_id and guild_id.

        Returns:
            JSON response containing member info and voice state.

        Async:
            This function is a coroutine and must be awaited.
        """
        try:
            userId = int(request.match_info["user_id"])
            guildId = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid params"}, status=400)

        guild = _resolveGuild(bot, guildId)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        member = guild.get_member(userId)
        if member is None:
            try:
                member = await guild.fetch_member(userId)
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

    async def sendMessage(request: web.Request) -> web.Response:
        """Post a message to a guild text channel.

        Args:
            request: Incoming HTTP request containing JSON body.

        Returns:
            JSON response with the created message ID.

        Side Effects:
            Sends a message to Discord.

        Async:
            This function is a coroutine and must be awaited.
        """
        try:
            data = await request.json()
            guildId = int(data["guild_id"])
            channelId = int(data["channel_id"])
            content = str(data["content"])
        except Exception:
            return web.json_response({"error": "invalid body"}, status=400)
        senderLabel = str(data.get("sender_label") or "TG")

        guild = _resolveGuild(bot, guildId)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)
        channel = guild.get_channel(channelId)
        if channel is None or not hasattr(channel, "send"):
            return web.json_response({"error": "channel not found"}, status=404)

        try:
            msg = await channel.send(f"**[TG/{senderLabel}]** {content}")
        except Exception as e:
            logger.exception("sendMessage failed")
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"message_id": msg.id})

    async def _pickAutoVoiceChannel(guild: discord.Guild) -> Optional[discord.VoiceChannel]:
        """Pick the most populated voice channel for autoplay."""
        candidates = [
            (ch, sum(1 for m in ch.members if not m.bot))
            for ch in guild.voice_channels
        ]
        candidates = [c for c in candidates if c[1] > 0]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    async def playAudio(request: web.Request) -> web.Response:
        """Play an uploaded audio file in a guild voice channel.

        Args:
            request: Incoming multipart request with file and guild_id.

        Returns:
            JSON response indicating playback target.

        Side Effects:
            Connects to voice, plays audio, and deletes the upload after playback.

        Async:
            This function is a coroutine and must be awaited.
        """
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"error": "expected multipart"}, status=400)

        reader = await request.multipart()
        guildId: Optional[int] = None
        targetChannelId: Optional[int] = None
        uploadPath: Optional[str] = None
        filename: Optional[str] = None

        downloadsDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(downloadsDir, exist_ok=True)

        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "guild_id":
                guildId = int((await part.read()).decode())
            elif part.name == "channel_id":
                raw = (await part.read()).decode().strip()
                if raw:
                    targetChannelId = int(raw)
            elif part.name == "file":
                ext = os.path.splitext(part.filename or "")[1] or ".ogg"
                filename = f"tg_{uuid.uuid4().hex}{ext}"
                uploadPath = os.path.join(downloadsDir, filename)
                with open(uploadPath, "wb") as f:
                    while chunk := await part.read_chunk():
                        f.write(chunk)

        if guildId is None or uploadPath is None:
            return web.json_response({"error": "missing guild_id or file"}, status=400)

        guild = _resolveGuild(bot, guildId)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if vc is None or not vc.is_connected():
            channel = None
            if targetChannelId:
                ch = guild.get_channel(targetChannelId)
                if isinstance(ch, discord.VoiceChannel):
                    channel = ch
            if channel is None:
                channel = await _pickAutoVoiceChannel(guild)
            if channel is None:
                try:
                    os.remove(uploadPath)
                except Exception:
                    pass
                return web.json_response({"error": "no active voice channel and no users in any voice channel"}, status=409)
            try:
                vc = await channel.connect(reconnect=True, timeout=10.0)
            except Exception as e:
                try:
                    os.remove(uploadPath)
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
                os.remove(uploadPath)
            except Exception:
                pass

        try:
            vc.play(discord.FFmpegOpusAudio(uploadPath), after=_cleanup)
        except Exception as e:
            _cleanup(None)
            return web.json_response({"error": f"play failed: {e}"}, status=500)

        return web.json_response({
            "played": True,
            "channel_id": vc.channel.id if vc.channel else None,
            "channel_name": vc.channel.name if vc.channel else None,
        })

    async def queue(request: web.Request) -> web.Response:
        """Return the current playback queue for a guild.

        Args:
            request: Incoming HTTP request with guild_id.

        Returns:
            JSON response with queue and playback state.

        Async:
            This function is a coroutine and must be awaited.
        """
        try:
            guildId = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "missing or invalid guild_id"}, status=400)
        gp = guildPlayers.get(guildId)
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
    app.router.add_post("/message", sendMessage)
    app.router.add_post("/play-audio", playAudio)
    app.router.add_get("/queue", queue)
    return app


async def startApiServer(bot: discord.Bot) -> web.AppRunner:
    """Start the aiohttp server for the HTTP API.

    Args:
        bot: Discord bot instance.

    Returns:
        The aiohttp AppRunner so callers can shut down the server.

    Side Effects:
        Binds a TCP socket and logs the listening address.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not config.API_SECRET:
        logger.warning("API_SECRET is empty - HTTP API will reject all requests. Set API_SECRET in .env to enable.")
    app = makeApp(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.API_HOST, port=config.API_PORT)
    await site.start()
    logger.info(f"HTTP API listening on http://{config.API_HOST}:{config.API_PORT}")
    return runner
