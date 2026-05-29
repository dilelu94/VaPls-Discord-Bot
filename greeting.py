import asyncio
import os
import time

import discord

import config

_last_soundboard_entry: dict[int, float] = {}
MILAPOLLO_PATH = os.path.join(config.CUSTOM_AUDIO_PATH, "Mila", "Milapollo.mp3")

async def trigger_soundboard_entry(channel):
    # Throttle: DAVE 4006 disconnects cause the bot to "rejoin" repeatedly,
    # and we don't want milapollo to fire each time.
    now = time.time()
    last = _last_soundboard_entry.get(channel.id, 0.0)
    if now - last < 60.0:
        return
    _last_soundboard_entry[channel.id] = now
    try:
        await asyncio.sleep(2)
        vc = channel.guild.voice_client
        if not vc or not vc.is_connected() or vc.is_playing():
            return
        if not os.path.exists(MILAPOLLO_PATH):
            return
        vc.play(discord.FFmpegOpusAudio(MILAPOLLO_PATH))
    except Exception: pass
