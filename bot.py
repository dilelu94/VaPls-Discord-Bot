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
    if hasattr(ctx, "response") and ctx.response.is_done():
        return True
    try:
        await ctx.defer()
        return True
    except Exception:
        return False


async def safe_respond(ctx, message):
    try:
        if ctx.response.is_done():
            await ctx.followup.send(message)
        else:
            await ctx.respond(message)
    except Exception:
        pass


async def safeEdit(ctx, message):
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
    log.info("Connected to Gateway. Starting command cleanup...")
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
    analytics.identify_user(ctx.author)
    props = {"command": name, "channel_id": str(getattr(ctx.channel, "id", "") or "")}
    if extra:
        props.update(extra)
    analytics.capture("command invoked", user=ctx.author, guild=ctx.guild, properties=props)


@bot.slash_command(name="parar")
async def parar(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "parar")
    await pararLogic(ctx)


@bot.slash_command(name="play", description="Reproduce una canción o playlist de YouTube")
async def play(ctx, query: discord.Option(str, description="Nombre de la canción o URL de YouTube")):
    await safe_defer(ctx)
    _track_command(ctx, "play", {"query_length": len(query or "")})
    await playLogic(ctx, query)


@bot.slash_command(name="soundpad")
async def soundpad(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "soundpad")
    await soundpadLogic(ctx)


@bot.slash_command(name="vapls", description="Preguntale al bot del server")
async def vapls(ctx, pregunta: discord.Option(str, description="Tu pregunta")):
    await safe_defer(ctx)
    _track_command(ctx, "vapls", {"prompt_length": len(pregunta or "")})
    await vaplsLogic(ctx, pregunta)


@bot.slash_command(name="indio", description="Charla con el indio")
async def indio(
    ctx,
    pregunta: discord.Option(str, description="Qué le decís al indio"),
    nuevo: discord.Option(bool, description="Empezar conversación nueva", required=False, default=False),
):
    await safe_defer(ctx)
    _track_command(ctx, "indio", {"prompt_length": len(pregunta or ""), "nuevo": bool(nuevo)})
    await indioLogic(ctx, pregunta, nuevo)


@bot.slash_command(name="quit", description="Sale del canal de voz")
async def quit(ctx):
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
