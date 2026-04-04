import discord
import asyncio
import os
import glob
import json
import audioop
import vosk
import logging
import sys
from discord.ext import commands
from keywords import check_keywords
import config

# Standard logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot')

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

# Fix for Python 3.12+ (and 3.14) where get_event_loop() doesn't auto-create a loop
# This must happen BEFORE any library call that expects a loop (like discord.Bot)
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print("DEBUG: Created new event loop for Python 3.14 compatibility.")

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

# Enable necessary intents for voice connection
intents = discord.Intents.default()
intents.voice_states = True

bot = discord.Bot(intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")

@bot.slash_command(name="escuchar", description="Escucha palabras clave")
async def escuchar(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        return await ctx.respond("❌ ¡Debes estar en un canal de voz!")

    await ctx.defer()
    channel = ctx.author.voice.channel
    
    # 1. FORCED CLEANUP: Disconnect from any zombie sessions
    if ctx.voice_client:
        print(f"DEBUG: Active voice client found. Disconnecting to ensure fresh session...")
        try:
            await ctx.voice_client.disconnect(force=True)
            await asyncio.sleep(2.0) # Longer wait for Discord to invalidate old session
        except Exception as e:
            print(f"DEBUG: Disconnect failed: {e}")

    print(f"DEBUG: Connecting to {channel.name}...")
    
    try:
        # 2. CONNECT: Explicitly handle connection
        vc = await channel.connect(timeout=30.0)
        
        # 3. ROBUST WAIT: Poll until the connection is fully handshake-complete
        connected = False
        for i in range(20): # Up to 10 seconds of polling
            if vc and vc.is_connected() and hasattr(vc, 'ws') and vc.ws:
                connected = True
                break
            await asyncio.sleep(0.5)

        if not connected:
            raise Exception("Timeout: Handshake de voz no completado tras 10s.")

        # Extra stabilization delay before starting sink
        await asyncio.sleep(1.0)
        print(f"DEBUG: Connection verified and stabilized for {channel.name}")
        
        # 4. START RECORDING
        vc.start_recording(
            KeywordDetectorSink(vc),
            lambda sink, *args: print("DEBUG: Recording stopped"),
            ctx.channel
        )
        await ctx.followup.send(f"🎙️ Escuchando en {channel.name}...")
        
    except discord.errors.ConnectionClosed as ce:
        print(f"DEBUG: ConnectionClosed Error {ce.code}: {ce.reason}")
        if ctx.voice_client:
            await ctx.voice_client.disconnect(force=True)
        await ctx.followup.send(f"❌ Error 4006 (Sesión inválida). Intenta usar `/escuchar` de nuevo.")
    except Exception as e:
        print(f"DEBUG: ERROR in escuchar: {type(e).__name__}: {e}")
        if ctx.voice_client:
            try:
                await ctx.voice_client.disconnect(force=True)
            except:
                pass
        await ctx.followup.send(f"❌ Error de conexión: {e}")

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
        await ctx.respond("❌ No estoy conectado.")

if __name__ == "__main__":
    if config.TOKEN:
        try:
            bot.run(config.TOKEN)
        except KeyboardInterrupt:
            print("Bot stopped by user.")
    else:
        print("Error: No TOKEN found.")
