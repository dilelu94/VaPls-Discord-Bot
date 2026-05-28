import discord
import analytics

async def pararLogic(ctx: discord.ApplicationContext):
    from bot import safe_respond
    from playCommand import guildPlayers, clearGuildPlayer

    if ctx.guild.id in guildPlayers:
        try:
            await guildPlayers[ctx.guild.id].stopPlayback()
        except Exception as e:
            print(f"[PARAR ERROR] Error stopping playback: {e}")
            analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                        properties={"action": "parar_stop_playback"})
        clearGuildPlayer(ctx.guild.id)

    if ctx.voice_client:
        channel_id = str(ctx.voice_client.channel.id)
        channel_name = ctx.voice_client.channel.name
        try:
            ctx.voice_client.stop_recording()
            setattr(ctx.voice_client, "recording", False)
            await ctx.voice_client.disconnect(force=True)
            analytics.capture("voice channel left", user=ctx.author, guild=ctx.guild,
                              properties={"channel_id": channel_id, "channel_name": channel_name,
                                          "trigger": "parar_command"})
            await safe_respond(ctx, "👋 Desconectado.")
        except Exception as e:
            analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                        properties={"action": "parar_disconnect"})
            await safe_respond(ctx, f"❌ Error al desconectar: {e}")
    else:
        await safe_respond(ctx, "❌ No conectado.")
