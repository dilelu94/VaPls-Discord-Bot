import asyncio
import os
import time

import discord

import config

# Map Discord user IDs to greeting audio paths relative to CUSTOM_AUDIO_PATH.
# Add entries here to give specific users their own greeting sound.
USER_GREETINGS: dict[int, str] = {
    285116759525031937: "Mila/Milapollo.mp3",  # Mila
}

DEFAULT_GREETING = os.path.join("Audios", "Fish Carrot.m4a")

_last_greeting: dict[int, float] = {}
_pending_trigger_user: dict[int, int] = {}


def set_pending_trigger(channel_id: int, user_id: int) -> None:
    _pending_trigger_user[channel_id] = user_id


def _resolve_greeting_path(user_id):
    rel = USER_GREETINGS.get(user_id) if user_id is not None else None
    if rel is None:
        rel = DEFAULT_GREETING
    return os.path.join(config.CUSTOM_AUDIO_PATH, rel)


async def trigger_soundboard_entry(channel):
    # Throttle: DAVE 4006 disconnects cause the bot to "rejoin" repeatedly,
    # and we don't want the greeting to fire each time.
    now = time.time()
    user_id = _pending_trigger_user.pop(channel.id, None)
    last = _last_greeting.get(channel.id, 0.0)
    if now - last < 60.0:
        return
    _last_greeting[channel.id] = now
    try:
        await asyncio.sleep(2)
        vc = channel.guild.voice_client
        if not vc or not vc.is_connected() or vc.is_playing():
            return
        path = _resolve_greeting_path(user_id)
        if not os.path.exists(path):
            return
        vc.play(discord.FFmpegOpusAudio(path))
    except Exception: pass
