import asyncio
import time

import discord

_last_soundboard_entry: dict[int, float] = {}

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
        sounds = await channel.guild.fetch_sounds()
        milapollo = discord.utils.find(lambda s: s.name.lower() == "milapollo", sounds)
        if milapollo: await channel.send_soundboard_sound(milapollo)
    except Exception: pass
