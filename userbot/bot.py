"""
VaPls userbot: listens to Discord voice channels using a real user account
(so DAVE E2EE works naturally) and transcribes Spanish speech with VOSK.

Runs separately from the main Discord bot — the main bot still handles
/play, /soundpad, slash commands, etc. This userbot is voice-input-only.
"""

import asyncio
import audioop
import json
import logging
import os
import sys
import time

import aiohttp
import discord  # discord.py-self installs as `discord`
import vosk

import config


# ---------- Logging --------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("userbot")
# Silence verbose internals from the library.
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)
logging.getLogger("discord.voice_client").setLevel(logging.INFO)


# ---------- VOSK setup -----------------------------------------------------

log.info(f"Loading Spanish VOSK model from {config.MODEL_PATH_ES} ...")
if not os.path.exists(config.MODEL_PATH_ES):
    log.error(f"Spanish model not found at {config.MODEL_PATH_ES}")
    sys.exit(1)
model_es = vosk.Model(config.MODEL_PATH_ES)
log.info("✅ Spanish VOSK model loaded.")


# ---------- Sink: VOSK transcription per speaking user ---------------------


class TranscriberSink(discord.sinks.Sink):
    """Per-user Spanish transcription. Receives raw PCM frames from the
    library's audio reader, downsamples 48k stereo → 16k mono, and feeds
    them to a dedicated KaldiRecognizer per speaking user."""

    def __init__(self, client, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        # Stubs the library inspects when wiring sink event listeners.
        self.__sink_listeners__ = []
        self.recognizers: dict[int, vosk.KaldiRecognizer] = {}
        self.resample_states: dict[int, object] = {}
        self.packet_count = 0
        self.start_time = time.time()

    def walk_children(self):
        return []

    def is_opus(self):
        # We want PCM, not Opus — the library should decode for us.
        return False

    def format_audio(self, audio):
        return audio

    def write(self, data, user):
        user_id = getattr(user, "id", user)
        if user_id in config.IGNORE_USER_IDS:
            return
        pcm_data = getattr(data, "pcm", data)
        if not isinstance(pcm_data, (bytes, bytearray)) or not pcm_data:
            return

        self.packet_count += 1
        if self.packet_count == 1:
            log.info(
                f"[VOSK] First packet received (user_id={user_id}, "
                f"bytes={len(pcm_data)})"
            )
        elif self.packet_count % 500 == 0:
            elapsed = time.time() - self.start_time
            log.info(
                f"[VOSK] {self.packet_count} packets in {elapsed:.1f}s "
                f"({self.packet_count / max(elapsed, 1):.0f} pkts/s)"
            )

        if user_id not in self.recognizers:
            self.recognizers[user_id] = vosk.KaldiRecognizer(model_es, 16000)
            self.resample_states[user_id] = None

        try:
            mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
            data_16k, new_state = audioop.ratecv(
                mono, 2, 1, 48000, 16000, self.resample_states[user_id]
            )
            self.resample_states[user_id] = new_state
            rec = self.recognizers[user_id]
            if rec.AcceptWaveform(data_16k):
                result = json.loads(rec.Result())
                text = result.get("text", "").strip()
                if text:
                    log.info(f"[VOSK][es] user_id={user_id}: {text}")
                    asyncio.run_coroutine_threadsafe(
                        on_transcript(user_id, text), self.client.loop
                    )
        except Exception as e:
            log.exception(f"[VOSK] write error: {e}")


# ---------- Optional downstream forwarding ---------------------------------


_http_session: aiohttp.ClientSession | None = None


async def _get_http() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def on_transcript(user_id: int, text: str):
    """Called every time the recognizer produces a final phrase."""
    # 1. Post to a configured text channel, if any.
    if config.TRANSCRIPT_CHANNEL_NAME:
        try:
            for guild in client.guilds:
                chan = discord.utils.get(
                    guild.text_channels, name=config.TRANSCRIPT_CHANNEL_NAME
                )
                if chan:
                    member = guild.get_member(user_id)
                    name = member.display_name if member else f"User {user_id}"
                    await chan.send(f"🎙️ **[ES] {name}:** {text}")
                    break
        except Exception as e:
            log.warning(f"text-channel post failed: {e}")

    # 2. Forward to the main bot's HTTP API, if enabled.
    if config.ENABLE_HTTP_FORWARD:
        try:
            session = await _get_http()
            headers = {}
            if config.BOT_API_SECRET:
                headers["X-API-Secret"] = config.BOT_API_SECRET
            await session.post(
                f"{config.BOT_API_BASE}/transcript",
                json={"user_id": str(user_id), "text": text, "language": "es"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            )
        except Exception as e:
            log.warning(f"HTTP forward failed: {e}")


# ---------- Discord client + auto-join logic -------------------------------

client = discord.Client(chunk_guilds_at_startup=False)


def _guild_allowed(guild_id: int) -> bool:
    return config.GUILD_ALLOWLIST is None or guild_id in config.GUILD_ALLOWLIST


async def _start_listening(vc: discord.VoiceClient):
    """Wait for the connection to stabilize, then start_recording."""
    if vc.is_recording():
        return
    for _ in range(40):
        if vc.is_connected():
            break
        await asyncio.sleep(0.5)
    else:
        log.warning(f"[VOICE] Timeout waiting for connection in {vc.channel.name}")
        return
    await asyncio.sleep(1.0)
    log.info(f"[VOICE] Starting listener in {vc.channel.name}")
    sink = TranscriberSink(client)
    try:
        vc.start_recording(sink, lambda *a, **kw: None)
    except Exception as e:
        log.exception(f"[VOICE] start_recording failed: {e}")


async def _join_channel(channel: discord.VoiceChannel) -> discord.VoiceClient | None:
    """Connect (or move) to a voice channel and start listening."""
    if not _guild_allowed(channel.guild.id):
        return None
    existing = discord.utils.get(client.voice_clients, guild=channel.guild)
    try:
        if existing:
            if existing.channel.id != channel.id:
                log.info(f"[VOICE] Moving from {existing.channel.name} → {channel.name}")
                await existing.move_to(channel)
            vc = existing
        else:
            log.info(f"[VOICE] Connecting to {channel.name} ({channel.guild.name})")
            vc = await channel.connect(reconnect=True, timeout=20.0)
    except Exception as e:
        log.exception(f"[VOICE] Failed to join {channel.name}: {e}")
        return None
    await _start_listening(vc)
    return vc


async def _leave_if_empty(guild: discord.Guild):
    """Disconnect from a guild's voice channel if no humans are left."""
    vc = discord.utils.get(client.voice_clients, guild=guild)
    if not vc:
        return
    humans = [m for m in vc.channel.members if not m.bot and m.id != client.user.id]
    if not humans:
        log.info(f"[VOICE] Channel {vc.channel.name} empty — leaving")
        try:
            vc.stop_recording()
        except Exception:
            pass
        try:
            await vc.disconnect(force=True)
        except Exception as e:
            log.warning(f"[VOICE] Disconnect error (ignored): {e}")


@client.event
async def on_ready():
    log.info(f"Userbot online as {client.user} (id={client.user.id})")
    # Auto-join any channel that already has humans in it.
    await asyncio.sleep(2)
    for guild in client.guilds:
        if not _guild_allowed(guild.id):
            continue
        for channel in guild.voice_channels:
            humans = [m for m in channel.members if not m.bot and m.id != client.user.id]
            if humans:
                await _join_channel(channel)
                break  # one channel per guild for now


@client.event
async def on_voice_state_update(member, before, after):
    if member.id == client.user.id:
        # Our own state change — ignore (we manage our own joins/leaves).
        return
    if member.bot or member.id in config.IGNORE_USER_IDS:
        # Don't follow bots or ignored users.
        return

    guild = (after.channel or before.channel).guild
    if not _guild_allowed(guild.id):
        return

    # A human joined a channel we're not in → follow them.
    if after.channel and (not before.channel or before.channel.id != after.channel.id):
        await _join_channel(after.channel)

    # A human left → if their old channel is now empty, leave too.
    if before.channel and (not after.channel or after.channel.id != before.channel.id):
        await _leave_if_empty(guild)


async def main():
    if not config.USER_TOKEN:
        log.error("USER_TOKEN is not set. See .env.example for setup instructions.")
        sys.exit(1)
    try:
        await client.start(config.USER_TOKEN)
    finally:
        if _http_session and not _http_session.closed:
            await _http_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
