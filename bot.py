import sys
import os
import logging
import warnings
import discord
import asyncio
import glob
import json
import time
import datetime
from playCommand import playLogic
from escucharCommand import escucharLogic
from pararCommand import pararLogic
from soundpadCommand import soundpadLogic
import audioop
import vosk
from discord.ext import commands
from keywords import checkKeywords
import config
import analytics
from apiServer import startApiServer

# DAVE workaround: py-cord 2.8 leaves packet.decrypted_data = None when DAVE is
# not ready or the ssrc is not yet mapped, so the reader silently drops the
# packet. We override decrypt_rtp to ALWAYS set packet.decrypted_data, falling
# back to the raw payload (post outer-AEAD) when DAVE can't decrypt yet.
from discord.voice.receive.reader import PacketDecryptor
from discord.voice.packets.core import OPUS_SILENCE
from nacl.exceptions import CryptoError
try:
    import davey
except ImportError:
    davey = None

def patched_decrypt_rtp(self, packet):
    state = self.client._connection
    dave = getattr(state, "dave_session", None)
    try:
        raw_payload = self._decryptor_rtp(packet)
    except (CryptoError, Exception):
        packet.decrypted_data = OPUS_SILENCE
        return OPUS_SILENCE

    if packet.padding and len(raw_payload) > 0:
        pad_len = raw_payload[-1]
        if 0 < pad_len <= len(raw_payload):
            raw_payload = raw_payload[:-pad_len]

    ssrc_map = getattr(state, "ssrc_user_map", None) or getattr(state, "_ssrc_to_id", {})
    uid = ssrc_map.get(packet.ssrc) if ssrc_map else None

    decrypted = None
    if dave is not None and getattr(dave, "ready", False) and uid and davey is not None:
        try:
            decrypted = dave.decrypt(uid, davey.MediaType.audio, raw_payload)
        except Exception:
            decrypted = None

    payload = decrypted if decrypted is not None else raw_payload
    if packet.extended:
        offset = packet.update_extended_header(payload)
        packet.decrypted_data = payload[offset:]
    else:
        packet.decrypted_data = payload
    return packet.decrypted_data

PacketDecryptor.decrypt_rtp = patched_decrypt_rtp

# Patch decode_packet to return silence on OpusError (corrupted stream)
original_decode_packet = discord.opus.PacketDecoder._decode_packet
def patched_decode_packet(self, packet):
    try:
        return original_decode_packet(self, packet)
    except Exception:
        # Return packet and 20ms of silence
        return packet, b"\x00" * 3840
discord.opus.PacketDecoder._decode_packet = patched_decode_packet

# Patch Decoder.decode to prevent crash
from discord.opus import Decoder
original_decoder_decode = Decoder.decode
def patched_decoder_decode(self, data, fec=False):
    try:
        return original_decoder_decode(self, data, fec)
    except Exception:
        return b"\x00" * 3840
Decoder.decode = patched_decoder_decode

# Standard logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s:%(name)s: %(message)s')

# Initialize Vosk models
model_es = None
model_en = None

def load_models():
    global model_es, model_en
    import psutil
    process = psutil.Process(os.getpid())
    def get_current_mem(): return process.memory_info().rss / (1024 * 1024)
    if os.path.exists(config.MODEL_PATH_ES):
        try: model_es = vosk.Model(config.MODEL_PATH_ES); print("✅ Spanish model loaded.")
        except Exception as e: print(f"❌ Error loading Spanish: {e}")
    if os.path.exists(config.MODEL_PATH_EN):
        mem = get_current_mem()
        if (config.RAM_THRESHOLD_MB - mem) > 100:
            try: model_en = vosk.Model(config.MODEL_PATH_EN); print("✅ English model loaded.")
            except Exception as e: print(f"❌ Error loading English: {e}")

load_models()

if not discord.opus.is_loaded():
    for lib in ['libopus.so.0', 'libopus.so', 'opus']:
        try: discord.opus.load_opus(lib); break
        except Exception: continue

async def safe_defer(ctx):
    if hasattr(ctx, "response") and ctx.response.is_done():
        return True
    try:
        await ctx.defer()
        return True
    except Exception:
        return False

async def safe_respond(ctx, message):
    try:
        if ctx.response.is_done(): await ctx.followup.send(message)
        else: await ctx.respond(message)
    except Exception: pass

async def safeEdit(ctx, message):
    try:
        if ctx.response.is_done(): await ctx.interaction.edit_original_response(content=message)
        else: await ctx.respond(message)
    except Exception: await safe_respond(ctx, message)

class KeywordDetectorSink(discord.sinks.Sink):
    def __init__(self, vc, **kwargs):
        super().__init__(**kwargs)
        self.vc = vc
        self.__sink_listeners__ = []
        self.recognizers = {}
        self.resample_states = {}
        self.packet_count = 0

    def walk_children(self): return []
    def is_opus(self): return False
    def format_audio(self, audio): return audio
    def write(self, data, user):
        user_id = getattr(user, 'id', user)
        pcm_data = getattr(data, 'pcm', data)
        if not isinstance(pcm_data, (bytes, bytearray)) or len(pcm_data) == 0: return
        self.packet_count += 1
        if self.packet_count == 1:
            logging.info(f"[VOSK] Primer paquete recibido (user_id={user_id}, bytes={len(pcm_data)})")
        elif self.packet_count % 250 == 0:
            logging.info(f"[VOSK] {self.packet_count} paquetes acumulados")

        if user_id not in self.recognizers:
            self.recognizers[user_id] = {}
            if model_es: self.recognizers[user_id]['es'] = vosk.KaldiRecognizer(model_es, 16000)
            if model_en: self.recognizers[user_id]['en'] = vosk.KaldiRecognizer(model_en, 16000)
            self.resample_states[user_id] = None

        try:
            mono = audioop.tomono(pcm_data, 2, 0.5, 0.5)
            data_16k, new_state = audioop.ratecv(mono, 2, 1, 48000, 16000, self.resample_states[user_id])
            self.resample_states[user_id] = new_state
            for lang, rec in self.recognizers[user_id].items():
                if rec.AcceptWaveform(data_16k):
                    result = json.loads(rec.Result())
                    text = result.get("text", "")
                    if text:
                        # logging.info goes through configured stream handler; print() can be lost
                        # to stdout buffering when running under systemd without PYTHONUNBUFFERED.
                        logging.info(f"[VOSK][{lang}] {user_id}: {text}")
                        analytics.capture("voice transcription captured", guild=getattr(self.vc, "guild", None),
                                          properties={"language": lang, "text_length": len(text),
                                                      "matched_keyword": checkKeywords(text)},
                                          distinct_id=str(user_id))
                        asyncio.run_coroutine_threadsafe(self.logToDiscord(user_id, text, lang), self.vc.client.loop)
                        if checkKeywords(text): self.triggerAudio(user_id, text); break
        except Exception: pass

    async def logToDiscord(self, user_id, text, lang):
        try:
            chan = discord.utils.get(self.vc.guild.text_channels, name="bot-testing")
            if chan:
                mbr = self.vc.guild.get_member(user_id)
                name = mbr.display_name if mbr else f"User {user_id}"
                await chan.send(f"🎙️ **[{lang.upper()}] {name}:** {text}")
        except Exception: pass

    def triggerAudio(self, userId, text):
        if self.vc.is_playing(): return
        text = text.lower()
        guild = getattr(self.vc, "guild", None)
        member = guild.get_member(userId) if guild else None
        if any(kw in text for kw in ["pedo", "caca", "fart"]):
            p = os.path.join(config.CUSTOM_AUDIO_PATH, "**/*Fart with reverb sound effect*.*")
            m = glob.glob(p, recursive=True)
            if m:
                try:
                    self.vc.play(discord.FFmpegOpusAudio(m[0]))
                    analytics.capture("keyword audio triggered", user=member, guild=guild,
                                      properties={"keyword_group": "fart", "matched_text": text[:120],
                                                  "audio_file": os.path.basename(m[0])})
                    return
                except Exception: pass
        keywords = text.split()
        for kw in keywords:
            p = os.path.join(config.CUSTOM_AUDIO_PATH, f"**/*{kw}*.*")
            m = glob.glob(p, recursive=True)
            if m:
                try:
                    self.vc.play(discord.FFmpegOpusAudio(m[0]))
                    analytics.capture("keyword audio triggered", user=member, guild=guild,
                                      properties={"keyword_group": "word_match", "keyword": kw,
                                                  "audio_file": os.path.basename(m[0])})
                    break
                except Exception: pass

intents = discord.Intents.default()
intents.voice_states = True
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
bot = discord.Bot(intents=intents, )

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


@bot.event
async def on_connect():
    print("DEBUG: Connected to Gateway. Starting command cleanup...")
    if config.DEBUG_GUILD_IDS:
        for guild_id in config.DEBUG_GUILD_IDS:
            try:
                # En py-cord, pasar un array vacio de comandos a sync_commands borra los de ese guild
                await bot.sync_commands(guild_ids=[guild_id], force=True)
                print(f"DEBUG: Cleaned up local commands for guild {guild_id}")
            except Exception as e:
                print(f"DEBUG: Error cleaning guild {guild_id}: {e}")
    print("DEBUG: Cleanup finished.")

_api_runner = None

@bot.event
async def on_ready():
    global _api_runner
    print(f"✅ Bot online as {bot.user}")
    await bot.sync_commands()
    asyncio.create_task(auto_join_existing_channels())
    if _api_runner is None:
        try:
            _api_runner = await startApiServer(bot)
        except Exception as e:
            print(f"⚠️ Failed to start HTTP API: {e}")

async def auto_join_existing_channels():
    await asyncio.sleep(2)
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            if any(not m.bot for m in channel.members):
                try:
                    vc = await channel.connect(reconnect=True, timeout=20.0)
                    await start_listening(vc)
                    return
                except Exception: pass

@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user:
        if not before.channel and after.channel:
            analytics.capture("voice channel joined", guild=after.channel.guild,
                              properties={"channel_id": str(after.channel.id),
                                          "channel_name": after.channel.name,
                                          "trigger": "state_update"})
            asyncio.create_task(trigger_soundboard_entry(after.channel))
        elif before.channel and not after.channel:
            analytics.capture("voice channel left", guild=before.channel.guild,
                              properties={"channel_id": str(before.channel.id),
                                          "channel_name": before.channel.name,
                                          "trigger": "state_update"})
        return
    if member.bot: return
    if after.channel and (not before.channel or before.channel != after.channel):
        vc = discord.utils.get(bot.voice_clients, guild=after.channel.guild)
        if vc:
            if vc.channel.id != after.channel.id: await vc.move_to(after.channel)
        else: vc = await after.channel.connect(reconnect=True)
        await start_listening(vc)

async def start_listening(vc):
    if vc.is_recording():
        return
    # Esperar a que la conexión se estabilice (hasta 20s).
    for _ in range(40):
        if vc.is_connected():
            break
        await asyncio.sleep(0.5)
    else:
        print(f"[VOICE] Timeout esperando conexión en {vc.channel.name}")
        return
    await asyncio.sleep(1.0)  # buffer post-handshake
    print(f"[VOICE] Starting listener in {vc.channel.name}")
    sink = KeywordDetectorSink(vc)
    try:
        vc.start_recording(sink, lambda *a, **kw: None)
    except Exception as e:
        print(f"[VOICE] start_recording falló: {e}")

def _track_command(ctx, name, extra=None):
    analytics.identify_user(ctx.author)
    props = {"command": name, "channel_id": str(getattr(ctx.channel, "id", "") or "")}
    if extra:
        props.update(extra)
    analytics.capture("command invoked", user=ctx.author, guild=ctx.guild, properties=props)


@bot.slash_command(name="escuchar")
async def escuchar(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "escuchar")
    await escucharLogic(ctx)

@bot.slash_command(name="parar")
async def parar(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "parar")
    await pararLogic(ctx)

@bot.slash_command(name="play", description="Reproduce una canción o playlist de YouTube")
async def play(ctx, query: discord.Option(str, description="Nombre de la canción o URL de YouTube")):
    await safe_defer(ctx)
    _track_command(ctx, "play", {"query_length": len(query or "")})
    await playLogic(ctx, query)

@bot.slash_command(name="soundpad")
async def soundpad(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "soundpad")
    await soundpadLogic(ctx)

@bot.slash_command(name="quit", description="Sale del canal de voz")
async def quit(ctx):
    await safe_defer(ctx)
    _track_command(ctx, "quit")

    vc = None
    for v in bot.voice_clients:
        if v.guild.id == ctx.guild.id:
            vc = v
            break

    if vc:
        channel_name = vc.channel.name
        channel_id = str(vc.channel.id)
        try:
            try:
                await asyncio.wait_for(vc.disconnect(force=True), timeout=5.0)
            except asyncio.TimeoutError:
                # Voice WS won't ack; tear down state directly so the slot frees.
                try:
                    vc.cleanup()
                except Exception:
                    pass
            analytics.capture("voice channel left", user=ctx.author, guild=ctx.guild,
                              properties={"channel_id": channel_id, "channel_name": channel_name,
                                          "trigger": "quit_command"})
            try:
                await ctx.followup.send(f"👋 Desconectado correctamente de {channel_name}.")
            except discord.NotFound:
                pass
        except Exception as e:
            analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                        properties={"action": "quit_disconnect"})
            try:
                await ctx.followup.send(f"⚠️ Error al desconectar: {e}")
            except Exception:
                pass
    else:
        try:
            await ctx.followup.send("❌ No estoy conectado a voz en este servidor.")
        except Exception:
            pass

@bot.slash_command(name="restart", description="devtool - no usar")
async def restart(ctx):
    _track_command(ctx, "restart")
    await ctx.respond("♻️ Reiniciando bot... (Esto cerrara el proceso actual)")
    print("[RESTART] Rebooting bot process...")
    analytics.shutdown()
    # Usar sys.executable para asegurar que usamos el mismo python/venv
    os.execv(sys.executable, [sys.executable, "/home/ubuntu/vapls-discord-bot/bot.py"])

if __name__ == "__main__":
    try:
        bot.run(config.TOKEN)
    finally:
        analytics.shutdown()
