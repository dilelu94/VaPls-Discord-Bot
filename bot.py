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
from discord.ext import commands
from keywords import check_keywords
import config

# Standard logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot')

# Suppress the DAVE protocol warning as we have davey installed
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

# Silence noisy Opus info logs
logging.getLogger("discord.opus").setLevel(logging.WARNING)

# Monkey-patch discord.opus.PacketDecoder._decode_packet
# This prevents the PacketRouter thread from dying when it encounters "corrupted stream"
# during DAVE handshake stabilization.
original_decode_packet = discord.opus.PacketDecoder._decode_packet
def patched_decode_packet(self, packet):
    try:
        return original_decode_packet(self, packet)
    except discord.opus.OpusError as e:
        if "corrupted stream" in str(e):
            # Return original packet and 20ms of silence
            return packet, b"\x00" * 3840
        raise e
discord.opus.PacketDecoder._decode_packet = patched_decode_packet

class KeywordDetectorSink(discord.sinks.WaveSink):
    __sink_listeners__ = []

    def __init__(self, vc, **kwargs):
        # WaveSink in py-cord 2.8+ only takes filters as keyword args
        super().__init__(**kwargs)
        self.vc = vc
        self.__sink_listeners__ = [] # Ensure instance also has it
        self.recognizers = {}
        self.resample_states = {}
        # Vocabularies to speed up recognition and reduce CPU usage
        self.vocab_es = '["necesito", "pito", "[unk]"]'
        self.vocab_en = '["i need", "whistle", "[unk]"]'

    def walk_children(self):
        return []

    def is_opus(self):
        return False

    def write(self, data, user_id):
        # In py-cord 2.8+, data might be a VoiceData object
        if hasattr(data, 'pcm'):
            data = data.pcm
        
        if not isinstance(data, (bytes, bytearray)):
            return

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
            # Py-cord 2.8+ feeds PCM data to write() for WaveSink
            # Convert to mono and resample to 16kHz for Vosk
            mono_data = audioop.tomono(data, 2, 0.5, 0.5)
            data_16k, new_state = audioop.ratecv(
                mono_data, 2, 1, 48000, 16000, self.resample_states[user_id]
            )
            self.resample_states[user_id] = new_state
            
            detected = False
            for lang, rec in self.recognizers[user_id].items():
                if rec.AcceptWaveform(data_16k):
                    result = json.loads(rec.Result())
                    text = result.get("text", "")
                    if text:
                        print(f"[TRANSCRIPTION][{lang}] User {user_id}: {text}")
                else:
                    partial = json.loads(rec.PartialResult())
                    text = partial.get("partial", "")
                
                if text and check_keywords(text):
                    detected = True
                    break
            
            if detected:
                self.trigger_audio(user_id, text)
        except Exception:
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

# Guild ID for testing
bot = discord.Bot(intents=intents, debug_guilds=[523359466528440320])

@bot.event
async def on_connect():
    print(f"DEBUG: Connected to Gateway. (Latency: {bot.latency*1000:.2f}ms)")

@bot.event
async def on_disconnect():
    print("WARNING: Disconnected from Discord Gateway.")

# Command sync flag to avoid spamming Discord
synced = False

@bot.event
async def on_ready():
    global synced
    print(f"✅ Bot is online as {bot.user}")
    if not synced:
        try:
            await bot.sync_commands()
            print("DEBUG: Commands synced successfully")
            synced = True
        except Exception as e:
            print(f"ERROR: Failed to sync commands: {e}")
    
    print(f"DEBUG: Active Guilds: {[(guild.name, guild.id) for guild in bot.guilds]}")
    # Application commands (Slash commands)
    print(f"DEBUG: Slash Commands registered: {[cmd.name for cmd in bot.application_commands]}")
    print(f"DEBUG: Pending Commands: {[cmd.name for cmd in bot.pending_application_commands]}")
    print(f"DEBUG: All commands: {list(bot.all_commands.keys())}")
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

# Regular function for recording finished callback in py-cord 2.8+
def on_record_finished(exception):
    if exception:
        print(f"DEBUG: Recording finished with error: {exception}")
    else:
        print("DEBUG: Recording session finished successfully.")

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
            print(f"DEBUG: Moving to {channel.name}")
            await ctx.voice_client.move_to(channel)
            vc = ctx.voice_client
    else:
        try:
            # VoiceClient handles DAVE protocol automatically in 2.8+ if davey is installed
            vc = await channel.connect(timeout=60.0, reconnect=True)
            print(f"DEBUG: Connected to {channel.name}")
        except Exception as e:
            print(f"[VOICE ERROR] Failed to connect: {e}")
            return await ctx.followup.send(f"❌ Error al conectar: {e}")

    try:
        # Stabilize connection and wait for DAVE handshake
        connected = False
        for i in range(120): # Longer timeout for DAVE
            if vc.is_connected() and vc.ws:
                connected = True
                break
            await asyncio.sleep(0.5)

        if not connected:
            raise Exception("Timeout: Voice connection did not stabilize.")

        # Extra delay for E2EE key exchange
        await asyncio.sleep(2.0)
        
        print(f"DEBUG: Voice connection stabilized. Starting KeywordDetectorSink.")
        vc.start_recording(
            KeywordDetectorSink(vc),
            on_record_finished
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
