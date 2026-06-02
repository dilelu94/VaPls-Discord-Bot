"""Slash command implementation for /parar (stop playback and disconnect)."""

import analytics


async def pararLogic(ctx):
    """Stop playback for the guild and disconnect the bot from voice.

    Args:
        ctx: Discord application context for the slash command.

    Returns:
        None.

    Side Effects:
        Stops any active GuildPlayer, disconnects voice, and emits analytics.

    Async:
        This function is a coroutine and must be awaited.
    """
    from bot import safe_respond
    from playCommand import guildPlayers, clearGuildPlayer
    from idleWatchdog import stop_idle_watchdog
    from soundpadCommand import disable_panels

    try:
        stop_idle_watchdog(ctx.guild.id)
    except Exception:
        pass

    if ctx.guild.id in guildPlayers:
        try:
            await guildPlayers[ctx.guild.id].stopPlayback()
        except Exception as e:
            print(f"[PARAR ERROR] Error stopping playback: {e}")
            analytics.capture_exception(
                e,
                user=ctx.author,
                guild=ctx.guild,
                properties={"action": "parar_stop_playback"},
            )
        clearGuildPlayer(ctx.guild.id)

    try:
        await disable_panels(ctx.guild.id)
    except Exception:
        pass

    if ctx.voice_client:
        channel_id = str(ctx.voice_client.channel.id)
        channel_name = ctx.voice_client.channel.name
        try:
            await ctx.voice_client.disconnect(force=True)
            analytics.capture(
                "voice channel left",
                user=ctx.author,
                guild=ctx.guild,
                properties={
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "trigger": "parar_command",
                },
            )
            await safe_respond(ctx, "👋 Desconectado.")
        except Exception as e:
            analytics.capture_exception(
                e,
                user=ctx.author,
                guild=ctx.guild,
                properties={"action": "parar_disconnect"},
            )
            await safe_respond(ctx, f"❌ Error al desconectar: {e}")
    else:
        await safe_respond(ctx, "❌ No conectado.")
