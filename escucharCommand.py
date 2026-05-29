import discord
import asyncio
import logging
import analytics
from greeting import set_pending_trigger

logger = logging.getLogger("bot.escuchar")

async def escucharLogic(ctx: discord.ApplicationContext):
    from bot import safe_defer, safe_respond, start_listening
    
    if not await safe_defer(ctx):
        return
    
    print(f"[COMMAND] /escuchar used by {ctx.author} in {ctx.guild.name}")
    
    if not ctx.author.voice:
        print(f"[COMMAND ERROR] {ctx.author} is not in a voice channel.")
        return await safe_respond(ctx, "❌ ¡Debes estar en un canal de voz!")

    channel = ctx.author.voice.channel
    print(f"DEBUG: Attempting to connect to channel: {channel.name} (ID: {channel.id})")

    # Check voice permissions
    permissions = channel.permissions_for(ctx.guild.me)
    if not (permissions.connect and permissions.speak):
        return await safe_respond(
            ctx,
            "❌ No tengo permiso para conectar o hablar en este canal. Por favor, verifica los permisos."
        )
    logger.info(f"DEBUG: Permisos del bot en {channel.name}: connect={permissions.connect}, speak={permissions.speak}")

    if ctx.voice_client:
        if ctx.voice_client.channel.id == channel.id:
            voiceClient = ctx.voice_client
            print("DEBUG: Already in the correct channel.")
            return await safe_respond(ctx, "🎙️ ¡Ya estoy escuchando!")
        else:
            print(f"DEBUG: Moving to {channel.name}")
            set_pending_trigger(channel.id, ctx.author.id)
            await ctx.voice_client.move_to(channel)
            await ctx.voice_client.edit(deafen=False)
            voiceClient = ctx.voice_client
    else:
        # Attempt connection with retries
        voiceClient = None
        for attempt in range(3):
            try:
                set_pending_trigger(channel.id, ctx.author.id)
                voiceClient = await channel.connect(reconnect=True)
                print(f"DEBUG: Connected to {channel.name} on attempt {attempt + 1}")
                break
            except discord.errors.Forbidden as e:
                logger.error(f"VOICE ERROR: Forbidden – {e}")
                return await safe_respond(ctx, "❌ Permiso denegado al intentar conectar al canal de voz.")
            except Exception as e:
                print(f"[VOICE ERROR] Intento {attempt + 1} de conexión falló: {e}")
                if attempt < 2:
                    await asyncio.sleep(5)
                else:
                    return await safe_respond(ctx, f"❌ Error al conectar después de {attempt + 1} intentos: {e}")
        
    # Guard: don't start a second recording session if already active.
    # start_listening() waits internally for the connection to stabilize.
    if voiceClient.is_recording():
        print("DEBUG: Already recording, skipping start_recording.")
        return await safe_respond(ctx, "🎙️ ¡Ya estoy escuchando!")

    # Start listening
    await start_listening(voiceClient)
    analytics.capture("voice channel joined", user=ctx.author, guild=ctx.guild,
                      properties={"channel_id": str(channel.id), "channel_name": channel.name,
                                  "trigger": "escuchar_command"})
    await safe_respond(ctx, f"🎙️ Escuchando en {channel.name}...")
