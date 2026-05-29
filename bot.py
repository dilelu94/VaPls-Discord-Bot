"""Main Discord bot entrypoint for VaPls.

Handles slash commands, voice playback, greeting triggers, analytics, and the
HTTP API server. Voice receive/transcription is delegated to the userbot in
./userbot/.
"""
import sys
import os
import logging
import asyncio
import time
import discord
from discord.ext import commands

from playCommand import playLogic
from pararCommand import pararLogic
from soundpadCommand import soundpadLogic
from geminiCommand import vaplsLogic, indioLogic
from greeting import trigger_soundboard_entry, set_pending_trigger
import config
import analytics
import apiServer
from apiServer import startApiServer

# Voice receive / VOSK transcription moved to the userbot in ./userbot/.
# This bot is now output-only: it joins voice channels solely to play music,
# soundboard sounds, or chat greetings via /play and /soundpad. The userbot
# (a real Discord account) handles audio capture and Spanish transcription
# because DAVE (Discord's E2EE) does not give bots the MLS keys.

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format='%(levelname)s:%(name)s: %(message)s',
)
log = logging.getLogger("bot")

if not discord.opus.is_loaded():
    for lib in ['libopus.so.0', 'libopus.so', 'opus']:
        try:
            discord.opus.load_opus(lib)
            break
        except Exception:
            continue


async def safe_defer(ctx):
    """Defer a Discord interaction if it has not been responded to yet.

    Args:
        ctx: Discord command context/interaction wrapper.

    Returns:
        True if defer succeeded or was already done, False otherwise.

    Side Effects:
        Sends a deferred response via Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    if hasattr(ctx, "response") and ctx.response.is_done():
        return True
    try:
        await ctx.defer()
        return True
    except Exception:
        return False


async def safe_respond(ctx, message):
    """Send a response or follow-up safely.

    Args:
        ctx: Discord command context/interaction wrapper.
        message: Message content to send.

    Side Effects:
        Sends a message to Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if ctx.response.is_done():
            await ctx.followup.send(message)
        else:
            await ctx.respond(message)
    except Exception:
        pass


async def safeEdit(ctx, message):
    """Edit the original response or fallback to responding.

    Args:
        ctx: Discord command context/interaction wrapper.
        message: Message content to send.

    Side Effects:
        Edits or sends a message via Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if ctx.response.is_done():
            await ctx.interaction.edit_original_response(content=message)
        else:
            await ctx.respond(message)
    except Exception:
        await safe_respond(ctx, message)


intents = discord.Intents.default()
intents.voice_states = True
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
bot = discord.Bot(intents=intents)


@bot.event
async def on_connect():
    """Sync per-guild commands on connect for debug guilds.

    Async:
        This function is a coroutine and must be awaited by the Discord client.
    """
    log.info("Connected to Gateway. Starting command cleanup...")
    apiServer._GATEWAY_CONNECTED_AT = time.time()
    if config.DEBUG_GUILD_IDS:
        for guild_id in config.DEBUG_GUILD_IDS:
            try:
                await bot.sync_commands(guild_ids=[guild_id], force=True)
                log.info(f"Cleaned up local commands for guild {guild_id}")
            except Exception as e:
                log.warning(f"Error cleaning guild {guild_id}: {e}")
    log.info("Cleanup finished.")


_api_runner = None


@bot.event
async def on_ready():
    """Finalize startup tasks and launch the HTTP API server.

    Async:
        This function is a coroutine and must be awaited by the Discord client.
    """
    global _api_runner
    log.info(f"Bot online as {bot.user}")
    await bot.sync_commands()
    if _api_runner is None:
        try:
            _api_runner = await startApiServer(bot)
        except Exception as e:
            log.warning(f"Failed to start HTTP API: {e}")


@bot.event
async def on_voice_state_update(member, before, after):
    """Track the bot's own voice state for analytics and greetings.

    Async:
        This function is a coroutine and must be awaited by the Discord client.
    """
    # The bot no longer auto-joins voice channels — that's the userbot's job
    # now. We only track the bot's own voice state for analytics and greetings
    # (when it joins via /play or /soundpad).
    if member != bot.user:
        return
    if not before.channel and after.channel:
        analytics.capture(
            "voice channel joined",
            guild=after.channel.guild,
            properties={
                "channel_id": str(after.channel.id),
                "channel_name": after.channel.name,
                "trigger": "state_update",
            },
        )
        asyncio.create_task(trigger_soundboard_entry(after.channel))
    elif before.channel and after.channel and before.channel.id != after.channel.id:
        asyncio.create_task(trigger_soundboard_entry(after.channel))
    elif before.channel and not after.channel:
        analytics.capture(
            "voice channel left",
            guild=before.channel.guild,
            properties={
                "channel_id": str(before.channel.id),
                "channel_name": before.channel.name,
                "trigger": "state_update",
            },
        )


def _track_command(ctx, name, extra=None):
    """Capture analytics for a slash command invocation.

    Args:
        ctx: Discord application context.
        name: Command name.
        extra: Optional dictionary of extra properties.

    Side Effects:
        Sends analytics events to PostHog when enabled.
    """
    analytics.identify_user(ctx.author)
    props = {"command": name, "channel_id": str(getattr(ctx.channel, "id", "") or "")}
    if extra:
        props.update(extra)
    analytics.capture("command invoked", user=ctx.author, guild=ctx.guild, properties=props)


@bot.slash_command(name="parar")
async def parar(ctx):
    """Slash command: stop playback and disconnect.

    Args:
        ctx: Discord application context.

    Side Effects:
        Stops playback via pararLogic and disconnects voice if needed.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "parar")
    await pararLogic(ctx)


@bot.slash_command(name="play", description="Reproduce una canción o playlist de YouTube")
async def play(ctx, query: discord.Option(str, description="Nombre de la canción o URL de YouTube")):
    """Slash command: queue and play a YouTube search or URL.

    Args:
        ctx: Discord application context.
        query: Search text or YouTube URL.

    Side Effects:
        Joins voice and starts the GuildPlayer playback flow.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "play", {"query_length": len(query or "")})
    await playLogic(ctx, query)


@bot.slash_command(name="soundpad")
async def soundpad(ctx):
    """Slash command: open the soundpad UI.

    Args:
        ctx: Discord application context.

    Side Effects:
        Connects to voice and sends an interactive view.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "soundpad")
    await soundpadLogic(ctx)


@bot.slash_command(name="vapls", description="Preguntale al bot del server")
async def vapls(ctx, pregunta: discord.Option(str, description="Tu pregunta")):
    """Slash command: ask the Gemini-backed VaPls persona.

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.

    Side Effects:
        Calls Gemini and sends the response back to Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "vapls", {"prompt_length": len(pregunta or "")})
    await vaplsLogic(ctx, pregunta)


@bot.slash_command(name="indio", description="Charla con el indio")
async def indio(
    ctx,
    pregunta: discord.Option(str, description="Qué le decís al indio"),
    nuevo: discord.Option(bool, description="Empezar conversación nueva", required=False, default=False),
):
    """Slash command: chat with the Indio persona (with history).

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.
        nuevo: Whether to reset the conversation history.

    Side Effects:
        Calls Gemini, updates history, and sends responses to Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "indio", {"prompt_length": len(pregunta or ""), "nuevo": bool(nuevo)})
    await indioLogic(ctx, pregunta, nuevo)


@bot.slash_command(name="quit", description="Sale del canal de voz")
async def quit(ctx):
    """Slash command: disconnect the bot from voice.

    Args:
        ctx: Discord application context.

    Side Effects:
        Disconnects the voice client and emits analytics.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "quit")

    vc = None
    for v in bot.voice_clients:
        if v.guild.id == ctx.guild.id:
            vc = v
            break

    if vc:
        channel_name = vc.channel.name
        channel_id = str(vc.channel.id)
        try:
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    vc.cleanup()
                except Exception:
                    pass
            analytics.capture(
                "voice channel left",
                user=ctx.author,
                guild=ctx.guild,
                properties={
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "trigger": "quit_command",
                },
            )
            try:
                await ctx.followup.send(f"👋 Desconectado correctamente de {channel_name}.")
            except discord.NotFound:
                pass
        except Exception as e:
            analytics.capture_exception(
                e,
                user=ctx.author,
                guild=ctx.guild,
                properties={"action": "quit_disconnect"},
            )
            try:
                await ctx.followup.send(f"⚠️ Error al desconectar: {e}")
            except Exception:
                pass
    else:
        try:
            await ctx.followup.send("❌ No estoy conectado a voz en este servidor.")
        except Exception:
            pass


@bot.slash_command(name="restart", description="devtool - no usar")
async def restart(ctx):
    """Slash command: restart the bot process (dev-only).

    Args:
        ctx: Discord application context.

    Side Effects:
        Calls os.execv to replace the current process.

    Async:
        This function is a coroutine and must be awaited.
    """
    _track_command(ctx, "restart")
    await ctx.respond("♻️ Reiniciando bot... (Esto cerrara el proceso actual)")
    log.info("[RESTART] Rebooting bot process...")
    analytics.shutdown()
    os.execv(sys.executable, [sys.executable, "/home/ubuntu/vapls-discord-bot/bot.py"])


if __name__ == "__main__":
    try:
        bot.run(config.TOKEN)
    finally:
        analytics.shutdown()
