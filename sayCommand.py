"""Slash command /say for TTS voice output in Discord voice channels."""

import os
import asyncio
import logging
import discord
import config
import analytics
import tts

_log = logging.getLogger("bot.say")


def _pick_populated_voice_channel(guild: discord.Guild):
    """Return the voice channel in ``guild`` with the most non-bot members, or None."""
    system_bots = {config.USERBOT_USER_ID, config.GOLIVE_USER_ID}
    candidates = [
        (
            ch,
            sum(
                1
                for m in ch.members
                if not getattr(m, "bot", True)
                and getattr(m, "id", None) not in system_bots
            ),
        )
        for ch in getattr(guild, "voice_channels", [])
    ]
    candidates = [c for c in candidates if c[1] > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


async def sayLogic(ctx, texto: str):
    """Slash command logic for /say <texto>.

    Args:
        ctx: Discord application context.
        texto: Message text to synthesize and speak in voice channel.
    """
    texto = (texto or "").strip()
    if not texto:
        return await ctx.respond("❌ Debes proporcionar un texto para decir.", ephemeral=True)

    if len(texto) > 300:
        return await ctx.respond("❌ El texto es demasiado largo (máximo 300 caracteres).", ephemeral=True)

    try:
        await ctx.defer()
    except Exception:
        pass

    guild = ctx.guild
    if not guild:
        return await ctx.respond("❌ Este comando solo puede usarse en un servidor.", ephemeral=True)

    # Check if user is in voice channel
    author_voice = getattr(ctx.author, "voice", None)
    target_channel = author_voice.channel if author_voice else None

    vc = guild.voice_client

    if vc is None or not vc.is_connected():
        if target_channel is None:
            target_channel = _pick_populated_voice_channel(guild)
        if target_channel is None:
            return await ctx.respond(
                "❌ Debes estar en un canal de voz para que el bot hable.",
                ephemeral=True
            )
        try:
            vc = await target_channel.connect(reconnect=True, timeout=10.0)
        except Exception as e:
            _log.warning("Failed to connect to voice channel %s: %s", target_channel, e)
            return await ctx.respond(f"❌ Error al conectar al canal de voz: {e}", ephemeral=True)
    elif target_channel is not None and getattr(vc.channel, "id", None) != target_channel.id:
        try:
            await vc.move_to(target_channel)
        except Exception as e:
            _log.warning("Failed to move to voice channel %s: %s", target_channel, e)

    # Generate TTS audio file
    wav_path = await asyncio.to_thread(tts.generate_tts_wav, texto)
    if not wav_path or not os.path.exists(wav_path):
        return await ctx.respond("❌ Error al generar la sintesis de voz TTS.", ephemeral=True)

    # Stop any current playback
    try:
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.1)
    except Exception:
        pass

    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _after(_err):
        try:
            loop.call_soon_threadsafe(done.set)
        except Exception:
            pass
        # Clean up temporary WAV file
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass

    try:
        vc.play(discord.FFmpegOpusAudio(wav_path), after=_after)
        analytics.capture(
            "say command executed",
            user=ctx.author,
            guild=ctx.guild,
            properties={"text_length": len(texto)}
        )
        return await ctx.respond(f"🗣️ **{ctx.author.display_name}**: {texto}")
    except Exception as e:
        _log.error("Failed to play TTS audio: %s", e)
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass
        return await ctx.respond(f"❌ Error al reproducir el audio: {e}", ephemeral=True)
