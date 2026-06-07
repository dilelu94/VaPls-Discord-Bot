"""HTTP API served alongside the main Discord bot.

Runs an aiohttp server in the same asyncio loop as py-cord. Every request must
include header X-API-Secret matching config.API_SECRET. Binds to
config.API_HOST:config.API_PORT (default 127.0.0.1:8080).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from urllib.parse import urljoin
import logging
from typing import Any, Optional

import aiohttp
import discord
from aiohttp import web

import analytics
import config
import geminiCommand
import geminiKeys
from playCommand import guildPlayers

logger = logging.getLogger("apiServer")

# Health/uptime counters surfaced by GET /status. _PROCESS_START is set when
# this module is imported; _GATEWAY_CONNECTED_AT is set by bot.py whenever the
# Discord gateway hands us a connected session (on_ready / on_connect).
_PROCESS_START = time.time()
_GATEWAY_CONNECTED_AT: Optional[float] = None


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
    # Admin routes use Basic Auth instead of X-API-Secret.
    if request.path.startswith("/admin"):
        return await handler(request)
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


async def _triggerUserbotRecording(
    *,
    guildId: int,
    channelId: int,
    callbackUrl: str,
    callbackSecret: Optional[str],
    metadataRaw: Optional[str],
    duration: float,
) -> None:
    """Ask the userbot's relay to capture a voice reply.

    Args:
        guildId: Guild that hosts the voice channel.
        channelId: Voice channel ID where the userbot should listen.
        callbackUrl: URL the userbot will POST the recorded audio to.
        callbackSecret: Optional X-API-Secret value for the callback request.
        metadataRaw: Opaque payload (typically JSON) forwarded to the
            callback so the Telegram bridge can route the audio back to the
            originating message.
        duration: Recording duration in seconds.

    Side Effects:
        Issues an HTTP POST to ``config.USERBOT_RECORD_URL``.

    Async:
        This function is a coroutine and must be awaited.
    """
    if not config.USERBOT_RECORD_URL:
        return
    payload: dict[str, Any] = {
        "guild_id": str(guildId),
        "channel_id": str(channelId),
        "duration": duration,
    }
    if callbackUrl:
        payload["callback_url"] = callbackUrl
    if callbackSecret:
        payload["callback_secret"] = callbackSecret
    if metadataRaw is not None:
        try:
            payload["callback_metadata"] = json.loads(metadataRaw)
        except (TypeError, ValueError):
            payload["callback_metadata"] = metadataRaw

    headers = {}
    if config.USERBOT_RECORD_SECRET:
        headers["X-API-Secret"] = config.USERBOT_RECORD_SECRET
    timeout = aiohttp.ClientTimeout(total=config.USERBOT_RECORD_TRIGGER_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                config.USERBOT_RECORD_URL,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "userbot /record HTTP %d: %s", resp.status, body[:200]
                    )
    except asyncio.TimeoutError:
        logger.warning(
            "userbot /record timeout after %.1fs", config.USERBOT_RECORD_TRIGGER_TIMEOUT
        )
    except Exception:
        logger.exception("userbot /record trigger failed")


def _resolveGuild(bot: discord.Bot, guildId: int) -> Optional[discord.Guild]:
    """Resolve a guild from the bot cache.

    Args:
        bot: Discord bot client.
        guildId: Guild ID to resolve.

    Returns:
        Guild instance if cached; otherwise None.
    """
    return bot.get_guild(guildId)


# ---- MMR Admin (proxied to userbot relay) ---------------------------------

_ADMIN_HTML = """<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8">
<title>VaPls MMR Admin</title>
<style>
body{background:#1a1a2e;color:#eee;font-family:sans-serif;margin:20px}
h1{color:#e94560}
table{width:100%;border-collapse:collapse;margin:10px 0}
td,th{border:1px solid #333;padding:8px;text-align:left}
th{background:#16213e}
input{background:#0f3460;color:#fff;border:1px solid #e94560;padding:4px;border-radius:3px}
button{background:#e94560;color:#fff;border:none;padding:6px 12px;border-radius:3px;cursor:pointer}
button:hover{background:#c73650}
.msg{display:none;padding:10px;margin:10px 0;border-radius:4px}
.msg.ok{background:#1b5e20;display:block}
.msg.err{background:#b71c1c;display:block}
.nav{display:flex;gap:10px;margin-bottom:20px}
.nav a{color:#e94560;text-decoration:none;padding:8px 16px;border:1px solid #e94560;border-radius:4px}
.nav a:hover{background:#e94560;color:#fff}
</style></head><body>
<h1>VaPls MMR Admin</h1>
<div class="nav">
  <a href="#" onclick="showTab('weights')">Weights</a>
  <a href="#" onclick="showTab('config')">Config</a>
  <a href="#" onclick="showTab('mmr')">MMR</a>
  <a href="#" onclick="showTab('activity')">Activity</a>
</div>
<div id="tab-weights" class="section"></div>
<div id="tab-config" class="section"></div>
<div id="tab-mmr" class="section"></div>
<div id="tab-activity" class="section"></div>
<div id="msg" class="msg"></div>
<script>
var AUTH = /*AUTH*/;
var allData = /*DATA*/;
function showTab(name) {
  document.querySelectorAll('.section').forEach(function(el) { el.style.display = 'none'; });
  document.getElementById('tab-' + name).style.display = 'block';
  renderTab(name);
}
function renderTab(name) {
  var el = document.getElementById('tab-' + name);
  if (name === 'weights') renderWeights(el);
  else if (name === 'config') renderConfig(el);
  else if (name === 'mmr') renderMmr(el);
  else if (name === 'activity') renderActivity(el);
}
function renderWeights(el) {
  var h = '<h2>Activity Weights</h2><table><tr><th>Activity</th><th>Weight</th><th>Action</th></tr>';
  for (var k in allData.config) {
    if (!k.startsWith('weight_')) continue;
    var act = k.slice(7);
    h += '<tr><td>' + act + '</td><td><input id="w-' + act + '" value="' + allData.config[k] + '" size="6"></td>'
      + '<td><button onclick="saveWeight(\'' + act + '\')">Save</button></td></tr>';
  }
  h += '</table>';
  el.innerHTML = h;
}
function renderConfig(el) {
  var h = '<h2>System Config</h2><table><tr><th>Key</th><th>Value</th><th>Action</th></tr>';
  for (var k in allData.config) {
    if (k.startsWith('weight_')) continue;
    h += '<tr><td>' + k + '</td><td><input id="c-' + k + '" value="' + allData.config[k] + '" size="10"></td>'
      + '<td><button onclick="saveConfig(\'' + k + '\')">Save</button></td></tr>';
  }
  h += '</table>';
  el.innerHTML = h;
}
function renderMmr(el) {
  var h = '<h2>MMR Rankings</h2><table><tr><th>User ID</th><th>Guild ID</th><th>Rating</th><th>Deviation</th><th>Activities</th><th>Premium</th></tr>';
  for (var i = 0; i < allData.mmr.length; i++) {
    var row = allData.mmr[i];
    h += '<tr><td>' + row.user_id + '</td><td>' + row.guild_id + '</td><td>' + row.rating + '</td><td>' + row.deviation + '</td><td>' + row.total_activities + '</td><td>' + (row.premium ? 'Y' : 'N') + '</td></tr>';
  }
  h += '</table>';
  el.innerHTML = h;
}
function renderActivity(el) {
  var h = '<h2>Recent Activity</h2><table><tr><th>ID</th><th>User</th><th>Type</th><th>Duration</th><th>Quality</th><th>Delta</th><th>Date</th></tr>';
  for (var i = 0; i < allData.activity.length; i++) {
    var row = allData.activity[i];
    var d = new Date((row.created_at || 0) * 1000).toLocaleString();
    h += '<tr><td>' + row.id + '</td><td>' + row.user_id + '</td><td>' + row.activity_type + '</td><td>' + (row.duration_secs || '-') + '</td><td>' + row.quality_score + '</td><td>' + (row.rating_delta || 0) + '</td><td>' + d + '</td></tr>';
  }
  h += '</table>';
  el.innerHTML = h;
}
function api(path, body) {
  var opts = {headers: {'Content-Type': 'application/json'}};
  if (AUTH) opts.headers.Authorization = AUTH;
  if (body) { opts.method = 'POST'; opts.body = JSON.stringify(body); }
  return fetch(path, opts);
}
async function saveWeight(act) {
  var v = document.getElementById('w-' + act).value;
  var r = await api('/admin/api/weights', {['weight_' + act]: v});
  if (!r.ok) { msg('Error saving', 'err'); return; }
  msg('Weight saved', 'ok');
  var data = await api('/admin/api/data');
  if (data.ok) allData = await data.json();
  renderTab(document.querySelector('.section[style*="block"]') ? 'weights' : 'weights');
}
async function saveConfig(k) {
  var v = document.getElementById('c-' + k).value;
  var r = await api('/admin/api/weights', {[k]: v});
  if (!r.ok) { msg('Error saving', 'err'); return; }
  msg('Config saved', 'ok');
  var data = await api('/admin/api/data');
  if (data.ok) allData = await data.json();
  renderTab(document.querySelector('.section[style*="block"]') ? 'config' : 'config');
}
showTab('weights');
function msg(text, type) {
  var el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + type;
  setTimeout(function() { el.style.display = 'none'; }, 3000);
}
</script></body></html>"""


def _checkAdminAuth(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, _, passwd = decoded.partition(":")
        return user == config.ADMIN_USER and passwd == config.ADMIN_PASS
    except Exception:
        return False


async def _admin_proxy(path: str, request: web.Request) -> web.Response:
    """Proxy a request to the userbot relay admin endpoint."""
    relay = config.INDIO_RELAY_URL
    if not relay:
        return web.json_response({"error": "relay not configured"}, status=503)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as sess:
            url = urljoin(relay, path)
            headers = {}
            auth = request.headers.get("Authorization")
            if auth:
                headers["Authorization"] = auth
            if request.method == "POST":
                body = await request.read()
                ct = request.headers.get("Content-Type", "application/json")
                async with sess.post(url, data=body, headers=headers) as resp:
                    data = await resp.read()
                    return web.Response(
                        status=resp.status,
                        body=data,
                        content_type=resp.content_type or "application/json",
                    )
            else:
                async with sess.get(url, headers=headers) as resp:
                    data = await resp.read()
                    return web.Response(
                        status=resp.status,
                        body=data,
                        content_type=resp.content_type or "application/json",
                    )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def makeApp(bot: discord.Bot) -> web.Application:
    """Create the aiohttp application with all API routes.

    Args:
        bot: Discord bot instance.

    Returns:
        Configured aiohttp Application.
    """
    app = web.Application(
        middlewares=[authMiddleware], client_max_size=25 * 1024 * 1024
    )

    async def status(_: web.Request) -> web.Response:
        """Return the bot readiness and voice client status.

        Returns:
            JSON response with readiness, guild count, and voice clients.

        Async:
            This function is a coroutine and must be awaited.
        """
        try:
            voiceStatesCount = sum(
                len(g.voice_states) if hasattr(g, "voice_states") else 0
                for g in bot.guilds
            )
        except (AttributeError, TypeError):
            voiceStatesCount = sum(
                sum(1 for ch in g.voice_channels for _ in ch.members)
                for g in bot.guilds
            )

        return web.json_response(
            {
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
                "uptime_seconds": int(time.time() - _PROCESS_START),
                "gateway_connected_seconds_ago": (
                    int(time.time() - _GATEWAY_CONNECTED_AT)
                    if _GATEWAY_CONNECTED_AT is not None
                    else None
                ),
                "voice_states_count": voiceStatesCount,
            }
        )

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
            return web.json_response(
                {"error": "missing or invalid guild_id"}, status=400
            )
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
        else:
            # Fallback: member.voice can be None even when the user *is*
            # in a voice channel (cache race / partial state). Scan the
            # guild's voice channels and rebuild a minimal voice dict.
            for ch in guild.voice_channels:
                if any(m.id == userId for m in ch.members):
                    voice = {
                        "channel_id": ch.id,
                        "channel_name": ch.name,
                        "self_mute": False,
                        "self_deaf": False,
                    }
                    break
        activity = None
        acts = getattr(member, "activities", None) or []
        if acts:
            activity = str(acts[0].name) if hasattr(acts[0], "name") else str(acts[0])

        top = getattr(member, "top_role", None)
        top_role = (
            {"name": top.name, "color": f"#{top.color.value:06x}"}
            if top and top.name != "@everyone"
            else None
        )

        return web.json_response(
            {
                "id": member.id,
                "display_name": member.display_name,
                "name": member.name,
                "status": str(getattr(member, "status", "unknown")),
                "activity": activity,
                "voice": voice,
                "roles": [r.name for r in member.roles],
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "created_at": member.created_at.isoformat(),
                "top_role": top_role,
            }
        )

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
            analytics.capture_exception(
                e,
                properties={
                    "action": "api_send_message",
                    "guild_id": guildId,
                    "channel_id": channelId,
                },
            )
            return web.json_response({"error": str(e)}, status=500)
        analytics.capture(
            "api message sent",
            properties={
                "guild_id": guildId,
                "channel_id": channelId,
                "sender_label": senderLabel,
            },
        )
        return web.json_response({"message_id": msg.id})

    async def _pickAutoVoiceChannel(
        guild: discord.Guild,
    ) -> Optional[discord.VoiceChannel]:
        """Pick the most populated voice channel for autoplay."""
        candidates = [
            (ch, sum(1 for m in ch.members if not m.bot)) for ch in guild.voice_channels
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
            Optionally triggers the userbot to record a voice reply once
            playback finishes.

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
        replyCallbackUrl: Optional[str] = None
        replyCallbackSecret: Optional[str] = None
        replyMetadata: Optional[str] = None
        replyDuration: Optional[float] = None

        downloadsDir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "downloads"
        )
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
            elif part.name == "reply_callback_url":
                raw = (await part.read()).decode().strip()
                if raw:
                    replyCallbackUrl = raw
            elif part.name == "reply_callback_secret":
                raw = (await part.read()).decode().strip()
                if raw:
                    replyCallbackSecret = raw
            elif part.name == "reply_metadata":
                raw = (await part.read()).decode()
                if raw.strip():
                    replyMetadata = raw
            elif part.name == "reply_duration":
                raw = (await part.read()).decode().strip()
                if raw:
                    try:
                        replyDuration = float(raw)
                    except ValueError:
                        return web.json_response(
                            {"error": "invalid reply_duration"}, status=400
                        )
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
                except Exception as e:
                    logger.warning("Failed to remove upload file: %s", e)
                return web.json_response(
                    {
                        "error": "no active voice channel and no users in any voice channel"
                    },
                    status=409,
                )
            try:
                vc = await channel.connect(reconnect=True, timeout=10.0)
            except Exception as e:
                try:
                    os.remove(uploadPath)
                except Exception as e2:
                    logger.warning("Failed to remove upload file: %s", e2)
                return web.json_response({"error": f"failed to join: {e}"}, status=500)

        try:
            if vc.is_playing():
                vc.stop()
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning("Failed to stop existing playback before playAudio: %s", e)

        recordChannelId = vc.channel.id if vc.channel else None
        wantRecording = bool(
            replyCallbackUrl
            and config.USERBOT_RECORD_URL
            and recordChannelId is not None
        )

        def _afterPlay(_err):
            try:
                os.remove(uploadPath)
            except Exception as e:
                logger.warning("Failed to remove upload in _afterPlay: %s", e)
            if not wantRecording:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    _triggerUserbotRecording(
                        guildId=guildId,
                        channelId=recordChannelId,
                        callbackUrl=replyCallbackUrl,
                        callbackSecret=replyCallbackSecret,
                        metadataRaw=replyMetadata,
                        duration=(
                            replyDuration
                            if replyDuration is not None
                            else config.USERBOT_RECORD_DEFAULT_DURATION
                        ),
                    ),
                    bot.loop,
                )
            except Exception:
                logger.exception("schedule userbot recording failed")

        try:
            vc.play(discord.FFmpegOpusAudio(uploadPath), after=_afterPlay)
        except Exception as e:
            _afterPlay(None)
            return web.json_response({"error": f"play failed: {e}"}, status=500)

        return web.json_response(
            {
                "played": True,
                "channel_id": recordChannelId,
                "channel_name": vc.channel.name if vc.channel else None,
                "will_record_reply": wantRecording,
            }
        )

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
            return web.json_response(
                {"error": "missing or invalid guild_id"}, status=400
            )
        gp = guildPlayers.get(guildId)
        if gp is None:
            return web.json_response(
                {
                    "current": None,
                    "queue": [],
                    "history_count": 0,
                    "is_paused": False,
                    "is_playing": False,
                }
            )
        vc = gp.vc
        return web.json_response(
            {
                "current": gp.currentSong,
                "queue": list(gp.queue),
                "history_count": len(gp.history),
                "is_paused": bool(vc and vc.is_paused()),
                "is_playing": bool(vc and vc.is_playing()),
            }
        )

    async def indioVoice(request: web.Request) -> web.Response:
        """Trigger the indio persona from a voice transcription.

        Body JSON: {pregunta, speaker_name, guild_id?, channel_id?,
        channel_name?, is_voice?}. When ``is_voice`` is true (default), the
        transcript is marked with a ``[voz] `` prefix so the indio knows to
        tolerate ASR errors, and a 1-in-N sampler may add ASR-quality
        feedback reactions to the transcript message. Returns immediately;
        the reply is delivered async.
        """
        try:
            data = await request.json()
            pregunta = str(data["pregunta"]).strip()
        except Exception:
            return web.json_response({"error": "invalid body"}, status=400)
        if not pregunta:
            return web.json_response({"error": "empty pregunta"}, status=400)
        speaker_name = data.get("speaker_name") or "alguien"
        guild_id = int(data["guild_id"]) if data.get("guild_id") else None
        channel_id = int(data["channel_id"]) if data.get("channel_id") else None
        channel_name = data.get("channel_name")
        # ``decifrar`` is kept as a back-compat alias for ``is_voice``: callers
        # that still send the old key keep working without changes.
        is_voice = bool(data.get("is_voice", data.get("decifrar", True)))
        user_id = int(data["user_id"]) if data.get("user_id") else 0
        transcript_message_id = (
            int(data["transcript_message_id"])
            if data.get("transcript_message_id")
            else None
        )
        source_message_id = (
            int(data["source_message_id"]) if data.get("source_message_id") else None
        )
        vosk_result = data.get("vosk_result")
        replied_content = data.get("replied_content")
        replied_author = data.get("replied_author")
        attachment_urls = data.get("attachment_urls")

        async def _run() -> None:
            text = pregunta
            # Vote-aware gating. When a music poll is live in this guild:
            #   - non-requester speakers are silently dropped (the userbot
            #     already filters them, but we backstop here in case of a
            #     relay-sync race or a userbot restart),
            #   - the requester's utterance is interpreted ONLY as a vote;
            #     non-vote text doesn't spawn askIndio (which would otherwise
            #     cascade a brand-new music vote on top of the open one).
            if guild_id is not None:
                import playCommand

                active_vote = playCommand.get_active_vote(int(guild_id))
                if active_vote is not None:
                    if (
                        active_vote.requester_id
                        and user_id
                        and int(user_id) != int(active_vote.requester_id)
                    ):
                        logger.info(
                            "indio voice: drop non-requester %s during open "
                            "vote (requester=%s) raw=%r",
                            user_id,
                            active_vote.requester_id,
                            text[:200],
                        )
                        return
                    if geminiCommand.try_register_voice_vote(
                        guild_id=guild_id,
                        user_id=user_id,
                        speaker_name=speaker_name,
                        text=text,
                    ):
                        logger.info(
                            "indio voice: registered vote from raw %r", text[:200]
                        )
                        return
                    logger.info(
                        "indio voice: drop requester non-vote during open vote: %r",
                        text[:200],
                    )
                    return
            if is_voice:
                # Probabilistic ASR-quality feedback: maybe add 👍/❌ reactions
                # to the transcript message so users can flag false positives.
                try:
                    import decifrarVoting

                    asyncio.create_task(
                        decifrarVoting.record(
                            text,
                            msg_id=transcript_message_id,
                            channel_id=channel_id,
                            vosk_result=vosk_result,
                        )
                    )
                except Exception:
                    logger.exception("indio voice: failed to schedule feedback record")
                # Tag the message so the indio knows it came from voice ASR
                # (so it tolerates phonetic errors instead of asking to repeat).
                text = f"[voz] {text}"
            await geminiCommand.askIndio(
                bot,
                text,
                speaker_name=speaker_name,
                guild_id=guild_id,
                channel_id=channel_id,
                channel_name=channel_name,
                user_id=user_id,
                source_message_id=source_message_id,
                is_voice=is_voice,
                replied_content=replied_content,
                replied_author=replied_author,
                attachment_urls=attachment_urls,
            )

        asyncio.create_task(_run())
        return web.json_response({"ok": True})

    async def playingState(request: web.Request) -> web.Response:
        """Return whether the main bot is currently playing audio in any voice channel.

        Used by the userbot to decide concurrency limits (3 vs 5 speakers).
        """
        playing_guilds = [vc.guild.id for vc in bot.voice_clients if vc.is_playing()]
        return web.json_response(
            {
                "is_playing": bool(playing_guilds),
                "guild_ids": [str(gid) for gid in playing_guilds],
            }
        )

    async def submitGeminiKey(request: web.Request) -> web.Response:
        """Receive one or more Gemini API keys from the userbot (or any
        loopback caller) and add them to the pool.

        Body JSON: {text, owner_id, owner_name, source?}. ``text`` is the raw
        DM content from which we extract candidate keys; ``owner_id`` /
        ``owner_name`` identify the donor. Returns a per-key breakdown so the
        caller can craft a reply.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid body"}, status=400)
        text = str(data.get("text") or "")
        owner_id = str(data.get("owner_id") or "")
        owner_name = str(data.get("owner_name") or "unknown")
        source = str(data.get("source") or "dm:userbot")
        candidates = geminiKeys.extract_keys_from_text(text)
        results: list[dict] = []
        for k in candidates:
            ok, reason = await geminiKeys.add_key(
                k,
                owner_id=owner_id,
                owner_name=owner_name,
                source=source,
            )
            results.append({"key_tail": k[-6:], "ok": ok, "reason": reason})
        return web.json_response(
            {
                "found": len(candidates),
                "results": results,
            }
        )

    async def textChannels(request: web.Request) -> web.Response:
        """List text channels of the guild."""
        try:
            guildId = int(request.query["guild_id"])
        except (KeyError, ValueError):
            return web.json_response(
                {"error": "missing or invalid guild_id"}, status=400
            )

        guild = _resolveGuild(bot, guildId)
        if guild is None:
            return web.json_response({"error": "guild not found"}, status=404)

        channels = [{"id": ch.id, "name": ch.name} for ch in guild.text_channels]
        return web.json_response({"text_channels": channels})

    async def githubWebhook(request: web.Request) -> web.Response:
        """Receive GitHub issue webhooks and sync groups with issue state.

        When an issue with the configured label is closed, the matching group
        is hidden. When it is reopened, the group is unhidden.

        Requires X-API-Secret header like all other endpoints. For production
        use, configure a reverse proxy (e.g. nginx) to inject the secret when
        forwarding from GitHub's webhook.
        """
        from suggestionsCommand import _store

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid body"}, status=400)

        # GitHub ping event — no issue/action, just a connectivity check
        if "zen" in data and "hook_id" in data:
            return web.json_response({"ok": True, "event": "ping"})

        action = data.get("action")
        issue = data.get("issue")
        if not isinstance(issue, dict):
            return web.json_response({"error": "missing issue"}, status=400)

        issue_number = issue.get("number")
        if not isinstance(issue_number, int):
            return web.json_response({"error": "missing issue number"}, status=400)

        issue_labels = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]
        if config.GITHUB_ISSUE_LABEL and config.GITHUB_ISSUE_LABEL not in issue_labels:
            return web.json_response({"ok": True, "skipped": "label mismatch"})

        store = _store()
        groups = await asyncio.to_thread(store.load)
        target = next((g for g in groups if g.issue_number == issue_number), None)
        if target is None:
            return web.json_response({"ok": True, "skipped": "no matching group"})

        if action == "closed" and not target.hidden:
            target.hidden = True
            await store.save(groups)
            logger.info(
                "github webhook: ocultado grupo %s (issue #%d cerrado)",
                target.id,
                issue_number,
            )
            return web.json_response({"ok": True, "result": "hidden"})

        if action == "reopened" and target.hidden:
            target.hidden = False
            await store.save(groups)
            logger.info(
                "github webhook: restaurado grupo %s (issue #%d reabierto)",
                target.id,
                issue_number,
            )
            return web.json_response({"ok": True, "result": "restored"})

        return web.json_response({"ok": True, "skipped": f"action={action}"})

    # ---- MMR Admin routes (Basic Auth) --------------------------------

    async def adminPage(request: web.Request) -> web.Response:
        if not _checkAdminAuth(request):
            resp = web.Response(status=401, text="Unauthorized")
            resp.headers["WWW-Authenticate"] = 'Basic realm="VaPls MMR Admin"'
            return resp
        auth = request.headers.get("Authorization", "") or ""
        data_json = "{}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                relay = config.INDIO_RELAY_URL
                if relay:
                    url = urljoin(relay, "/admin/api/data")
                    hdrs = {"Authorization": auth} if auth else {}
                    async with sess.get(url, headers=hdrs) as resp:
                        if resp.status == 200:
                            data_json = await resp.text()
        except Exception:
            data_json = "{}"
        html = _ADMIN_HTML.replace("/*AUTH*/", json.dumps(auth)).replace(
            "/*DATA*/", data_json
        )
        return web.Response(text=html, content_type="text/html")

    async def adminData(request: web.Request) -> web.Response:
        if not _checkAdminAuth(request):
            resp = web.Response(status=401, text="Unauthorized")
            resp.headers["WWW-Authenticate"] = 'Basic realm="VaPls MMR Admin"'
            return resp
        return await _admin_proxy("/admin/api/data", request)

    async def adminWeights(request: web.Request) -> web.Response:
        if not _checkAdminAuth(request):
            resp = web.Response(status=401, text="Unauthorized")
            resp.headers["WWW-Authenticate"] = 'Basic realm="VaPls MMR Admin"'
            return resp
        return await _admin_proxy("/admin/api/weights", request)

    app.router.add_get("/admin", adminPage)
    app.router.add_get("/admin/api/data", adminData)
    app.router.add_post("/admin/api/weights", adminWeights)

    app.router.add_get("/status", status)
    app.router.add_get("/members", members)
    app.router.add_get("/user/{user_id}", user)
    app.router.add_post("/message", sendMessage)
    app.router.add_post("/play-audio", playAudio)
    app.router.add_get("/queue", queue)
    app.router.add_post("/indio", indioVoice)
    app.router.add_get("/playing", playingState)
    app.router.add_post("/gemini-key", submitGeminiKey)
    app.router.add_get("/channels", textChannels)
    app.router.add_post("/github-webhook", githubWebhook)
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
        logger.warning(
            "API_SECRET is empty - HTTP API will reject all requests. Set API_SECRET in .env to enable."
        )
    app = makeApp(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.API_HOST, port=config.API_PORT)
    await site.start()
    logger.info(f"HTTP API listening on http://{config.API_HOST}:{config.API_PORT}")
    return runner
