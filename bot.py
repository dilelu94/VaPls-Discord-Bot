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
from urllib.parse import urljoin
import aiohttp
import discord
from discord.ext import commands

from playCommand import playLogic, openDjMenu
from pararCommand import pararLogic
from soundpadCommand import soundpadLogic, soundpad_query_autocomplete
from geminiCommand import vaplsLogic, indioLogic
from suggestionsCommand import sugerenciasLogic, sugerenciasVerLogic
from greeting import trigger_soundboard_entry, set_pending_trigger
import config
import analytics
import apiServer
from apiServer import startApiServer
import decifrarVoting
import errorHandler
import geminiKeys
from idleWatchdog import start_idle_watchdog, stop_idle_watchdog
import webhookLogger

# Voice receive / VOSK transcription moved to the userbot in ./userbot/.
# This bot is now output-only: it joins voice channels solely to play music,
# soundboard sounds, or chat greetings via /play and /soundpad. The userbot
# (a real Discord account) handles audio capture and Spanish transcription
# because DAVE (Discord's E2EE) does not give bots the MLS keys.

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(levelname)s:%(name)s: %(message)s",
)
log = logging.getLogger("bot")

import posthog_client

posthog_client.init_observability(service_name="vapls-main-bot")

# Forward logs to a Discord thread via webhook (LOG_WEBHOOK_URL env var).
# Disabled when the env var is empty — silent no-op.
_webhook_log_handler = webhookLogger.install_from_env("bot")

if not discord.opus.is_loaded():
    for lib in ["libopus.so.0", "libopus.so", "opus"]:
        try:
            discord.opus.load_opus(lib)
            break
        except Exception:
            continue


async def safe_defer(ctx, ephemeral: bool = False):
    """Defer a Discord interaction if it has not been responded to yet.

    Args:
        ctx: Discord command context/interaction wrapper.
        ephemeral: If True, the deferred response (and all subsequent
            followups) are visible only to the invoker. Once an interaction
            is deferred public, followup ``ephemeral=True`` is silently
            ignored by Discord — pick the flag here.

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
        await ctx.defer(ephemeral=ephemeral)
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


geminiKeys.load_from_disk()

intents = discord.Intents.default()
intents.voice_states = True
# Necesario para que on_message reciba DMs (handler que detecta API keys
# de Gemini cuando los users se las mandan al bot por privado).
intents.messages = True
intents.dm_messages = True
intents.message_content = True
# Necesario para contar votos por reacción en la votación de música del indio
# (on_raw_reaction_add). Está en Intents.default() pero lo dejamos explícito.
intents.reactions = True
# Sin esto, member.status siempre es "offline" y member.activities siempre
# vacío en /user/<id>. Requiere activar "PRESENCE INTENT" en el Developer Portal.
intents.presences = True
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
    if _webhook_log_handler is not None:
        _webhook_log_handler.start(asyncio.get_running_loop())
    await bot.sync_commands()
    if _api_runner is None:
        try:
            _api_runner = await startApiServer(bot)
        except Exception as e:
            log.warning(f"Failed to start HTTP API: {e}")
    try:
        await decifrarVoting.start(bot)
    except Exception:
        log.exception("decifrar voting startup failed")


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
        try:
            start_idle_watchdog(bot, after.channel.guild.id)
        except Exception:
            log.exception("failed to start idle watchdog")
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
        try:
            stop_idle_watchdog(before.channel.guild.id)
        except Exception:
            log.exception("failed to stop idle watchdog")
        # If a GuildPlayer still has a currentSong, this disconnect was
        # involuntary (kick, network drop, /quit) — /parar would have removed
        # the entry from guildPlayers via clearGuildPlayer. Snapshot the
        # elapsed position so the next /play (or indio resume_music) can
        # restart from where we left off.
        try:
            from playCommand import guildPlayers

            _player = guildPlayers.get(before.channel.guild.id)
            if _player is not None and _player.currentSong is not None:
                _player.mark_interrupted()
        except Exception:
            log.exception("failed to mark player as interrupted")


@bot.event
async def on_message(message):
    """DM handler that absorbs Gemini API keys.

    When a user DMs the bot and the message contains one or more strings that
    look like Gemini API keys (``AIzaSy…`` or ``AQ.Ab8RN6…``), we hot-add them
    to the pool and reply with a short confirmation crediting the donor.
    Messages in guild channels (slash commands, anything else) are ignored —
    this is purely an opt-in donation channel.
    """
    if message.author is None or message.author.bot:
        return
    if message.guild is not None:
        return  # solo DMs
    content = (message.content or "").strip()
    if not content:
        return
    found = geminiKeys.extract_keys_from_text(content)
    if not found:
        return
    owner_id = str(message.author.id)
    owner_name = getattr(message.author, "display_name", None) or getattr(
        message.author, "name", "unknown"
    )
    added: list[str] = []
    dupes: list[str] = []
    failed: list[tuple[str, str]] = []
    for k in found:
        ok, reason = await geminiKeys.add_key(
            k,
            owner_id=owner_id,
            owner_name=owner_name,
            source="dm:bot",
        )
        if ok:
            added.append(k)
        elif reason == "already in pool":
            dupes.append(k)
        else:
            failed.append((k, reason))
    lines: list[str] = []
    if added:
        lines.append(f"✅ Sumé {len(added)} key(s) al pool. ¡Gracias {owner_name}!")
    if dupes:
        lines.append(f"ℹ️ {len(dupes)} key(s) ya estaban cargadas.")
    if failed:
        lines.append(
            "❌ Algunas no pude sumarlas:\n" + "\n".join(f"- {r}" for _, r in failed)
        )
    if lines:
        try:
            await message.channel.send("\n".join(lines))
        except Exception:
            log.exception("on_message: reply failed")
    log.info(
        "gemini key DM from %s (%s): added=%d dupes=%d failed=%d",
        owner_name,
        owner_id,
        len(added),
        len(dupes),
        len(failed),
    )


@bot.event
async def on_raw_reaction_add(payload):
    """Route emoji reactions to the relevant subsystems.

    Two consumers:
      - ``geminiCommand.register_reaction_vote``: counts keycap reactions on
        an open music-vote options message.
      - ``decifrarVoting.handle_reaction_vote``: resolves 👍/❌ on sampled
        voice-transcript messages (ASR-quality feedback).
    """
    try:
        if bot.user is not None and payload.user_id == bot.user.id:
            return
        import geminiCommand

        geminiCommand.register_reaction_vote(
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            emoji=str(payload.emoji),
            user_id=payload.user_id,
        )
        import decifrarVoting

        await decifrarVoting.handle_reaction_vote(
            bot,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            emoji=str(payload.emoji),
            user_id=payload.user_id,
            added=True,
        )
    except Exception:
        log.exception("on_raw_reaction_add failed")


@bot.event
async def on_application_command_error(ctx, error):
    """Red de seguridad para excepciones no atrapadas por los comandos.

    Los comandos atrapan sus propios errores con mensajes específicos
    (yt-dlp en playCommand, GeminiError en geminiCommand). Este handler
    solo se dispara cuando algo se escapa — evita que la interaction
    quede colgada en "thinking..." sin respuesta.
    """
    await errorHandler.handle(ctx, error)


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
    analytics.capture(
        "command invoked", user=ctx.author, guild=ctx.guild, properties=props
    )


@bot.slash_command(name="dj", description="Abre el menú del modo DJ en el canal de música")
async def dj(ctx):
    """Slash command: open the Auto-DJ menu in the configured music channel.

    Args:
        ctx: Discord application context.

    Side Effects:
        Posts the DJ menu (DjMenuView) in AUTODJ_MENU_CHANNEL_ID with buttons
        to activate, veto, fire, or stop the Auto-DJ mode.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "dj")
    if ctx.guild is None:
        await ctx.followup.send("❌ Este comando solo funciona en un servidor.", ephemeral=True)
        return
    ok, msg = await openDjMenu(ctx.bot, ctx.guild.id)
    if not ok:
        await ctx.followup.send(f"❌ No pude abrir el menú DJ: {msg}", ephemeral=True)
    else:
        try:
            await ctx.followup.send("🎧 Menú DJ abierto.", ephemeral=True)
        except Exception:
            pass


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


@bot.slash_command(name="queue", description="Muestra la cola de reproducción actual")
async def queueCommand(ctx):
    """Slash command: render the current queue as an ephemeral embed."""
    from playCommand import guildPlayers, build_queue_embed

    _track_command(ctx, "queue")
    player = guildPlayers.get(ctx.guild.id) if ctx.guild else None
    embed = build_queue_embed(player)
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(
    name="play", description="Reproduce una canción o playlist de YouTube"
)
async def play(
    ctx,
    query: discord.Option(
        str,
        description="Nombre de la canción o URL de YouTube",
        required=False,
        default=None,
    ) = None,
):
    """Slash command: queue and play a YouTube search or URL.

    Args:
        ctx: Discord application context.
        query: Search text or YouTube URL. If empty, replies with a hint
            instead of starting playback.

    Side Effects:
        Joins voice and starts the GuildPlayer playback flow.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "play", {"query_length": len(query or "")})
    if not query or not query.strip():
        await ctx.followup.send("decime qué reproducir la próxima", ephemeral=True)
        return
    await playLogic(ctx, query)


@bot.slash_command(
    name="soundpad", description="Abre el panel o reproduce un clip por nombre"
)
async def soundpad(
    ctx,
    query: discord.Option(
        str,
        description="Nombre aproximado del clip a reproducir (vacío = abrir panel)",
        required=False,
        default=None,
        autocomplete=soundpad_query_autocomplete,
    ) = None,
):
    """Slash command: open the soundpad UI or play a clip by fuzzy name.

    Args:
        ctx: Discord application context.
        query: Optional search string. When provided, the bot finds the
            closest-matching clip and plays it directly instead of opening
            the panel.

    Side Effects:
        Connects to voice and either sends an interactive view or plays a clip.

    Async:
        This function is a coroutine and must be awaited.
    """
    # Gate before defer: a synchronous in-memory check (geminiKeys.has_user_key)
    # is cheap enough to run inside the 3s interaction window, so users without
    # a donated key get an immediate ephemeral instead of seeing Discord's
    # "thinking…" first and the rejection second.
    if not geminiKeys.has_user_key(ctx.author.id):
        contributors = geminiKeys.format_contributors_line()
        msg = (
            "🔒 Para usar **/soundpad** necesitás aportar una API key de Gemini al pool del bot.\n\n"
            f"**Cómo conseguirla:** entrá a {config.GEMINI_KEYS_DONATION_URL}, "
            "clickeá *Create API key* (es gratis con una cuenta de Google) "
            "y mandámela por DM al bot. Apenas la sumo al pool podés usar el comando."
        )
        if contributors:
            msg = f"{msg}\n\n{contributors}"
        analytics.capture(
            "soundpad gated",
            user=ctx.author,
            guild=ctx.guild,
            properties={"reason": "no_user_key"},
        )
        await ctx.respond(msg, ephemeral=True)
        return

    await safe_defer(ctx)
    _track_command(ctx, "soundpad", {"query_length": len(query or "")})
    await soundpadLogic(ctx, query=query)


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
    charla: discord.Option(str, description="Qué le decís al indio"),
):
    """Slash command: chat with the Indio persona (with history).

    Args:
        ctx: Discord application context.
        charla: User message to the Indio.

    Side Effects:
        Calls Gemini, updates history, and sends responses to Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    # Cuando el override de canal esta activo y el slash se invoca desde otro
    # canal, avisar al invocador ephemeral (solo lo ve el que disparó /indio)
    # que la respuesta va al target. El defer también va ephemeral, porque si
    # no Discord ignora el ephemeral=True del followup.
    source_id = getattr(ctx, "channel_id", None) or getattr(
        getattr(ctx, "channel", None), "id", None
    )
    target_id = config.INDIO_REPLY_CHANNEL_ID
    will_redirect = bool(target_id and source_id and source_id != target_id)
    await safe_defer(ctx, ephemeral=will_redirect)
    _track_command(ctx, "indio", {"prompt_length": len(charla or "")})
    if will_redirect:
        try:
            await ctx.followup.send(
                f"te respondo en <#{target_id}>",
                ephemeral=True,
            )
        except Exception:
            log.exception("indio: source-channel ack failed")
    await indioLogic(ctx, charla, False)


@bot.slash_command(
    name="sugerencias",
    description="Sugerile algo al bot — se agrupa con ideas similares",
)
async def sugerencias(
    ctx,
    idea: discord.Option(str, description="Tu idea, cambio o feature deseado"),
):
    """Slash command: submit a free-form suggestion to the bot.

    Args:
        ctx: Discord application context.
        idea: User-provided suggestion text.

    Side Effects:
        Persists the suggestion to disk (grouped with similar prior ideas via
        Gemini Flash-Lite) and replies ephemerally to the user.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=True)
    except Exception:
        pass
    _track_command(ctx, "sugerencias", {"idea_length": len(idea or "")})
    await sugerenciasLogic(ctx, idea)


@bot.slash_command(
    name="sugerencias-ver",
    description="Mirá qué sugerencias ya existen, ordenadas por las más pedidas",
)
async def sugerencias_ver(ctx):
    """Slash command: list existing suggestion groups ranked by demand.

    Args:
        ctx: Discord application context.

    Side Effects:
        Reads the persisted suggestions and replies ephemerally with the
        ranked listing.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=True)
    except Exception:
        pass
    _track_command(ctx, "sugerencias-ver", {})
    await sugerenciasVerLogic(ctx)


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
                stop_idle_watchdog(ctx.guild.id)
            except Exception:
                pass
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
                await ctx.followup.send(
                    f"👋 Desconectado correctamente de {channel_name}."
                )
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


@bot.slash_command(
    name="entraindio", description="Hace que el indio entre a tu canal de voz"
)
async def entraindio(ctx):
    """Slash command: ask the userbot to join the caller's voice channel.

    Args:
        ctx: Discord application context.

    Side Effects:
        Sends an HTTP request to the userbot relay (``/join``) which makes
        the real-user Indio account connect to the caller's voice channel.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "entraindio")

    voice_state = getattr(ctx.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None) if voice_state else None
    if voice_channel is None:
        await safe_respond(
            ctx, "❌ Tenés que estar en un canal de voz para que el indio entre."
        )
        return

    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        await safe_respond(ctx, "❌ El relay del indio no está configurado.")
        return

    join_url = urljoin(config.INDIO_RELAY_URL, "/join")
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    payload = {"channel_id": int(voice_channel.id)}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(join_url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    log.warning("entraindio relay HTTP %s: %s", resp.status, body[:200])
                    await safe_respond(
                        ctx, f"⚠️ El indio no pudo entrar (HTTP {resp.status})."
                    )
                    return
    except Exception as e:
        log.exception("entraindio relay failed")
        await safe_respond(ctx, f"⚠️ Error llamando al indio: {e}")
        return

    await safe_respond(ctx, f"🪶 El indio va para **{voice_channel.name}**.")


@bot.slash_command(
    name="sensibilidad",
    description="Cambia la sensibilidad del wake-word del indio (presets 1-3)",
)
async def sensibilidad(
    ctx,
    preset: discord.Option(
        int,
        description="Preset: 1=más sensible, 2=solo 'che indio' (default), 3=placeholder",
        choices=[1, 2, 3],
    ),
):
    """Slash command: switch the VOSK wake-word sensitivity preset.

    Args:
        ctx: Discord application context.
        preset: Integer 1-3 selecting the sensitivity preset.

    Side Effects:
        POSTs to the userbot relay ``/sensibilidad`` which updates the active
        wake-word pattern set and rebuilds the VOSK grammar in-memory.

    Async:
        This function is a coroutine and must be awaited.
    """
    await safe_defer(ctx)
    _track_command(ctx, "sensibilidad")

    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        await safe_respond(ctx, "❌ El relay del indio no está configurado.")
        return

    url = urljoin(config.INDIO_RELAY_URL, "/sensibilidad")
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    payload = {"preset": preset}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    log.warning(
                        "sensibilidad relay HTTP %s: %s", resp.status, body[:200]
                    )
                    await safe_respond(
                        ctx, f"⚠️ No pude cambiar la sensibilidad (HTTP {resp.status})."
                    )
                    return
    except Exception as e:
        log.exception("sensibilidad relay failed")
        await safe_respond(ctx, f"⚠️ Error llamando al indio: {e}")
        return

    _PRESET_DESCRIPTIONS = {
        1: "**Preset 1** — más sensible: `che indio`, `que indio`, `eh indio` + verbos.",
        2: '**Preset 2** — menos sensible (default): solo `che indio` + verbos. Reduce falsos positivos de "que".',
        3: "**Preset 3** — (placeholder / WIP): igual al preset 2 por ahora.",
    }
    await safe_respond(
        ctx, f"🎙️ Sensibilidad actualizada → {_PRESET_DESCRIPTIONS[preset]}"
    )


@bot.slash_command(
    name="huh",
    description="Activa/desactiva el sonido de confirmación al detectar wake-word — hecho con ayuda de chipotlai",
)
async def huh(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "huh")

    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        await safe_respond(ctx, "❌ El relay del indio no está configurado.")
        return

    url = urljoin(config.INDIO_RELAY_URL, "/toggle_wake_sound")
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json={}, headers=headers) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    await safe_respond(ctx, "⚠️ No pude cambiar el estado del sonido.")
                    return
                enabled = body.get("enabled", False)
    except Exception as e:
        log.exception("huh relay failed")
        await safe_respond(ctx, f"⚠️ Error llamando al indio: {e}")
        return

    status = "✅ Activado" if enabled else "❌ Desactivado"
    await safe_respond(ctx, f"🎵 Sonido de wake-word: {status}")


@bot.slash_command(
    name="help", description="Lista los comandos del bot y cómo funciona"
)
async def help_cmd(ctx):
    """Slash command: list available commands and bot/userbot info.

    Args:
        ctx: Discord application context.

    Side Effects:
        Sends an ephemeral embed with the command list and contributors.

    Async:
        This function is a coroutine and must be awaited.
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=True)
    except Exception:
        pass
    _track_command(ctx, "help")

    embed = discord.Embed(
        title="🎙️ VaPls — ayuda",
        description=(
            "Bot de voz/música + persona Gemini con memoria. "
            "Corre en **dos procesos**:\n"
            "• **Main bot** (este) — slash commands, música, soundpad, Gemini.\n"
            "• **Userbot (Indio)** — escucha voz en canales E2EE, transcribe "
            "con faster-whisper y responde al wake-word *indio*."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="🎵 Música y voz",
        value=(
            "**/play** `query` — busca o pega una URL de YouTube y la "
            "reproduce. Con varios resultados muestra menú.\n"
            "**/soundpad** `[query]` — abre el panel de clips locales, o "
            "reproduce el que más se parezca a `query`.\n"
            "**/entraindio** — hace que el indio (userbot) entre a tu canal "
            "de voz para escuchar y responder al wake-word.\n"
            "**/sensibilidad** `1|2|3` — ajusta la sensibilidad del wake-word "
            "(1=máxima, 2=solo 'che indio', 3=WIP).\n"
            "**/parar** — corta la reproducción, limpia la cola y se "
            "desconecta.\n"
            "**/quit** — sale del canal de voz sin tocar la cola."
        ),
        inline=False,
    )
    embed.add_field(
        name="🤖 Gemini",
        value=(
            "**/vapls** `pregunta` — respuesta puntual, sin memoria.\n"
            "**/indio** `charla` — persona con memoria corta por guild y "
            "memoria larga destilada (rasgos, anécdotas, chistes internos). "
            "También responde por voz cuando lo nombrás en un canal donde "
            "está el userbot."
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Otros",
        value=(
            "**/sugerencias** `idea` — mandá una sugerencia o feature; se "
            "agrupa con ideas parecidas.\n"
            "**/sugerencias-ver** — mirá qué sugerencias ya existen, ordenadas "
            "por las más pedidas.\n"
            "**/help** — esto."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔑 API keys de Gemini",
        value=(
            "El pool de keys está bancado por la comunidad. Si querés "
            "sumar la tuya, mandámela por **DM al bot** "
            "(formato `AIzaSy…` o `AQ.Ab8RN6…`). Se suma en caliente, sin "
            "reinicio, y queda asociada a tu user para darte crédito."
        ),
        inline=False,
    )
    contributors = geminiKeys.format_contributors_line()
    if contributors:
        embed.set_footer(text=contributors)
    try:
        await ctx.followup.send(embed=embed, ephemeral=True)
    except Exception:
        await safe_respond(ctx, "No pude mandar el help — fijate los logs.")


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
