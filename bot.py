import discord
import asyncio
import os
import glob
import json
import audioop
import vosk
from discord.ext import commands
from keywords import check_keywords
import config

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

# Fix for Python 3.12+ where get_event_loop() doesn't auto-create a loop
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

class KeywordDetectorSink(discord.sinks.WaveSink):
    def __init__(self, vc, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vc = vc
        # Store dict of dicts: user_id -> {'es': rec, 'en': rec}
        self.recognizers = {}
        self.resample_states = {}
        # Restricted vocabularies
        self.vocab_es = '["necesito", "pito", "[unk]"]'
        self.vocab_en = '["i need", "whistle", "[unk]"]'

    def write(self, data, user_id):
        super().write(data, user_id)

        # Skip if no models are loaded
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
            # 1. Convert to Mono
            mono_data = audioop.tomono(data, 2, 0.5, 0.5)
            # 2. Downsample to 16000 Hz using persistent state
            data_16k, new_state = audioop.ratecv(
                mono_data, 2, 1, 48000, 16000, self.resample_states[user_id]
            )
            self.resample_states[user_id] = new_state
            
            # 3. Process with each recognizer
            detected = False
            for lang, rec in self.recognizers[user_id].items():
                if rec.AcceptWaveform(data_16k):
                    text = json.loads(rec.Result()).get("text", "")
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "")
                
                if text and check_keywords(text):
                    detected = True
                    break # Trigger once if detected in any language
            
            if detected:
                self.trigger_audio()
                
        except Exception as e:
            # Print error to console for debugging but don't crash the sink
            # print(f"Error processing audio for {user_id}: {e}")
            pass

    def trigger_audio(self):
        if self.vc.is_playing():
            return

        pattern = os.path.join(config.AUDIO_DIR, "necesitopito.*")
        matches = glob.glob(pattern)
        if not matches:
            return

        audio_path = matches[0]
        try:
            self.vc.play(discord.FFmpegOpusAudio(audio_path))
        except Exception as e:
            print(f"Error playing audio: {e}")

bot = discord.Bot()

@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")

@bot.slash_command(name="escuchar", description="Escucha palabras clave en español e inglés")
async def escuchar(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        return await ctx.respond("❌ ¡Debes estar en un canal de voz!")

    channel = ctx.author.voice.channel
    try:
        vc = await channel.connect()
    except discord.ClientException:
        vc = ctx.voice_client

    vc.start_recording(
        KeywordDetectorSink(vc),
        lambda sink, *args: print("Recording stopped"),
        ctx.channel
    )
    
    await ctx.respond(f"🎙️ Escuchando en {channel.name}...")

@bot.slash_command(name="parar", description="Detiene la grabación y desconecta")
async def parar(ctx: discord.ApplicationContext):
    if not ctx.voice_client:
        return await ctx.respond("❌ No estoy en un canal de voz.")

    vc = ctx.voice_client
    vc.stop_recording()
    await vc.disconnect()
    await ctx.respond("👋 Desconectado.")

if __name__ == "__main__":
    if not config.TOKEN:
        print("Error: TOKEN not found in environment or .env file.")
    else:
        bot.run(config.TOKEN)
