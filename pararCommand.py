import discord

async def pararLogic(ctx: discord.ApplicationContext):
    from bot import safe_respond
    from playCommand import guildPlayers, clearGuildPlayer
    
    if ctx.guild.id in guildPlayers:
        try:
            await guildPlayers[ctx.guild.id].stopPlayback()
        except Exception as e:
            print(f"[PARAR ERROR] Error stopping playback: {e}")
        clearGuildPlayer(ctx.guild.id)
        
    if ctx.voice_client:
        try:
            ctx.voice_client.stop_recording()
            setattr(ctx.voice_client, "recording", False)
            await ctx.voice_client.disconnect(force=True)
            await safe_respond(ctx, "👋 Desconectado.")
        except Exception as e:
            await safe_respond(ctx, f"❌ Error al desconectar: {e}")
    else:
        await safe_respond(ctx, "❌ No conectado.")
