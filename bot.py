import sys
import os
import logging
import warnings
import discord
import asyncio
import glob
import json
import audioop
import vosk
import threading
import aiohttp
from discord.ext import commands
from discord.gateway import DiscordVoiceWebSocket
from keywords import check_keywords
import config

# Monkeypatch DiscordVoiceWebSocket to support DAVE protocol (E2EE)
# Required since the current py-cord version (2.6.1) doesn't have it natively.

original_identify = DiscordVoiceWebSocket.identify

async def patched_identify(self):
    print("DEBUG: Using patched_identify with DAVE support")
    state = self._connection
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "server_id": str(state.server_id),
            "user_id": str(state.user.id),
            "session_id": state.session_id,
            "token": state.token,
            "max_dave_protocol_version": 1,
        },
    }
    await self.send_as_json(payload)

DiscordVoiceWebSocket.identify = patched_identify

original_from_client = DiscordVoiceWebSocket.from_client

@classmethod
async def patched_from_client(cls, client, *, resume=False, hook=None):
    """Creates a voice websocket for the :class:`VoiceClient` with v=7 for DAVE support."""
    print(f"DEBUG: Using patched_from_client for endpoint {client.endpoint}")
    gateway = f"wss://{client.endpoint}/?v=7"
    http = client._state.http
    socket = await http.ws_connect(gateway, compress=15)
    ws = cls(socket, loop=client.loop, hook=hook)
    ws.gateway = gateway
    ws._connection = client
    ws._max_heartbeat_timeout = 60.0
    ws.thread_id = threading.get_ident()

    if resume:
        await ws.resume()
    else:
        await ws.identify()

    return ws

DiscordVoiceWebSocket.from_client = patched_from_client

# Standard logging
...
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot')

# Suppress the DAVE protocol warning
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Voice reception is currently broken")

# Initialize Vosk models
model_es = None
model_en = None

if os.path.exists(config.MODEL_PATH_ES):
    model_es = vosk.Model(config.MODEL_PATH_ES)
else:
    print(f"Warning: Spanish model not found at {config.MODEL_PATH_ES}")

if os.path.exists(config.MODEL_PATH_EN):
    model_en = vosk.Model(config.MODEL_PATH_EN)
else:
    print(f"Warning: English model not found at {config.MODEL_PATH_EN}")

# Ensure libopus is loaded
if not discord.opus.is_loaded():
    for lib in ['libopus.so.0', 'libopus.so', 'opus']:
        try:
            discord.opus.load_opus(lib)
            print(f"DEBUG: Loaded opus: {lib}")
            break
        except Exception:
            continue

# Event loop fix for Python 3.12+
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

class KeywordDetectorSink(discord.sinks.WaveSink):
    def __init__(self, vc, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vc = vc
        self.recognizers = {}
        self.resample_states = {}
        self.vocab_es = '["necesito", "pito", "[unk]"]'
        self.vocab_en = '["i need", "whistle", "[unk]"]'

    def write(self, data, user_id):
        super().write(data, user_id)
        if model_es is None and model_en is None:
            return

        if user_id not in self.recognizers:
            self.recognizers[user_id] = {}
            if model_es:
                self.recognizers[user_id]['es'] = vosk.KaldiRecognizer(model_es, 16000, self.vocab_es)
            if model_en:
                self.recognizers[user_id]['en'] = vosk.KaldiRecognizer(model_en, 16000, self.vocab_en)
            self.resample_states[user_id] = None

        try:
            mono_data = audioop.tomono(data, 2, 0.5, 0.5)
            data_16k, new_state = audioop.ratecv(
                mono_data, 2, 1, 48000, 16000, self.resample_states[user_id]
            )
            self.resample_states[user_id] = new_state
            
            detected = False
            for lang, rec in self.recognizers[user_id].items():
                if rec.AcceptWaveform(data_16k):
                    text = json.loads(rec.Result()).get("text", "")
                    if text:
                        print(f"[TRANSCRIPTION][{lang}] User {user_id}: {text}")
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "")
                
                if text and check_keywords(text):
                    detected = True
                    break
            
            if detected:
                self.trigger_audio(user_id, text)
        except Exception as e:
            pass

    def trigger_audio(self, user_id, detected_text):
        if self.vc.is_playing():
            return
        print(f"[BOT ACTION] Detected '{detected_text}' from User {user_id}. Playing audio.")
        pattern = os.path.join(config.AUDIO_DIR, "necesitopito.*")
        matches = glob.glob(pattern)
        if matches:
            try:
                self.vc.play(discord.FFmpegOpusAudio(matches[0]))
            except Exception as e:
                print(f"Error playing audio: {e}")

# Enable necessary intents
intents = discord.Intents.default()
intents.voice_states = True

# Guild ID del servidor de pruebas
bot = discord.Bot(intents=intents, debug_guilds=[523359466528440320])

@bot.event
async def on_connect():
    print(f"DEBUG: Connected to Gateway. (Latencia: {bot.latency*1000:.2f}ms)")

@bot.event
async def on_disconnect():
    print("WARNING: Disconnected from Discord Gateway.")

@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    try:
        await bot.sync_commands()
        print("DEBUG: Commands synced successfully")
    except Exception as e:
        print(f"ERROR: Failed to sync commands: {e}")
    print(f"DEBUG: Active Guilds: {[(guild.name, guild.id) for guild in bot.guilds]}")
    # Application commands (Slash commands)
    print(f"DEBUG: Slash Commands registered: {[cmd.name for cmd in bot.application_commands]}")
    print(f"DEBUG: Pending Commands: {[cmd.name for cmd in bot.pending_application_commands]}")
    print(f"DEBUG: All commands (bot.all_commands): {list(bot.all_commands.keys())}")
    print("--- Ready to receive commands ---")

@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user:
        if before.channel and not after.channel:
            print(f"DEBUG: Bot was disconnected from voice channel: {before.channel.name}")
        elif not before.channel and after.channel:
            print(f"DEBUG: Bot joined voice channel: {after.channel.name}")
        elif before.channel != after.channel:
            print(f"DEBUG: Bot moved voice channel from {before.channel.name} to {after.channel.name}")

@bot.slash_command(name="escuchar", description="Escucha palabras clave")
async def escuchar(ctx: discord.ApplicationContext):
    await ctx.defer()
    print(f"[COMMAND] /escuchar used by {ctx.author} in {ctx.guild.name}")
    
    if not ctx.author.voice:
        print(f"[COMMAND ERROR] {ctx.author} is not in a voice channel.")
        return await ctx.followup.send("❌ ¡Debes estar en un canal de voz!")

    channel = ctx.author.voice.channel
    print(f"DEBUG: Attempting to connect to channel: {channel.name} (ID: {channel.id})")
    
    if ctx.voice_client:
        if ctx.voice_client.channel.id == channel.id:
            print("DEBUG: Already in the correct channel.")
            return await ctx.followup.send("🎙️ ¡Ya estoy escuchando!")
        else:
            print(f"DEBUG: Moving from {ctx.voice_client.channel.name} to {channel.name}")
            await ctx.voice_client.move_to(channel)
            vc = ctx.voice_client
    else:
        try:
            vc = await channel.connect(timeout=60.0, reconnect=True)
            print(f"DEBUG: Connected to {channel.name}")
        except Exception as e:
            print(f"[VOICE ERROR] Failed to connect: {e}")
            return await ctx.followup.send(f"❌ Error al conectar: {e}")

    try:
        connected = False
        for i in range(40):
            if vc.is_connected() and hasattr(vc, 'ws') and vc.ws:
                connected = True
                break
            await asyncio.sleep(0.5)

        if not connected:
            raise Exception("Timeout: La voz no se estabilizó.")

        print(f"DEBUG: Voice connection stabilized. Starting KeywordDetectorSink.")
        vc.start_recording(
            KeywordDetectorSink(vc),
            lambda sink, *args: print(f"DEBUG: Sink for {channel.name} finished."),
            ctx.channel
        )
        await ctx.followup.send(f"🎙️ Escuchando en {channel.name}...")
        
    except Exception as e:
        print(f"[VOICE ERROR] Post-connection failure: {e}")
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=True)
        await ctx.followup.send(f"❌ Error de voz: {e}")

@bot.slash_command(name="parar", description="Detiene y desconecta")
async def parar(ctx: discord.ApplicationContext):
    if ctx.voice_client:
        try:
            ctx.voice_client.stop_recording()
            await ctx.voice_client.disconnect(force=True)
            await ctx.respond("👋 Desconectado.")
        except Exception as e:
            await ctx.respond(f"❌ Error al desconectar: {e}")
    else:
        await ctx.respond("❌ No conectado.")

if __name__ == "__main__":
    if config.TOKEN:
        try:
            bot.run(config.TOKEN)
        except Exception as e:
            print(f"Bot fatal error: {e}")
    else:
        print("Error: No TOKEN found.")
