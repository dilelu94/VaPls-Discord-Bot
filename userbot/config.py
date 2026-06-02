"""Runtime configuration for the voice transcription userbot."""
import os
from dotenv import load_dotenv

load_dotenv()

# Discord user account token (NOT a bot token). Obtain from DevTools →
# Network → any request → headers → authorization. Treat as a secret.
USER_TOKEN = os.getenv("USER_TOKEN")

# --- faster-whisper transcription -----------------------------------------
# Model name (e.g. "tiny", "base", "small") or a HuggingFace repo. Resolved
# via faster-whisper's standard download path. On the Ampere A1 4/24 server,
# "small" runs comfortably real-time with concurrency 5; "base" was the cap
# on the old 1 GB E2.1.Micro. "tiny" produces garbage on rioplatense audio.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
# Quantization preset: "int8" runs on CPU with low memory; "float16" needs GPU.
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
# Directory where faster-whisper caches downloaded models. Empty = library default.
WHISPER_CACHE_DIR = os.getenv("WHISPER_CACHE_DIR", "")
# CTranslate2 thread count for inference. Match the VM vCPU count (4 on the
# Ampere A1 4/24 server) for best per-utterance throughput.
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "4"))

# Concurrency caps: how many overlapping utterances may be transcribed at
# once. When the main bot is playing audio (music/soundpad), we throttle to
# leave CPU headroom for ffmpeg + the playback pipeline.
MAX_CONCURRENT_IDLE = int(os.getenv("MAX_CONCURRENT_IDLE", "5"))
MAX_CONCURRENT_WHILE_PLAYING = int(os.getenv("MAX_CONCURRENT_WHILE_PLAYING", "3"))

# --- Wake-word gating (VOSK pre-filter + Whisper on demand) ---------------
# When WAKE_WORD_ENABLED is true, the userbot uses a tiny VOSK recognizer with
# a restricted grammar (only "indio" / "che indio" / "[unk]") to watch every
# speaker continuously. Whisper only runs on a captured chunk AFTER VOSK has
# heard the wake word — saves CPU and Gemini quota, and keeps the transcript
# channel clean (no more posting every random utterance).
WAKE_WORD_ENABLED = os.getenv("WAKE_WORD_ENABLED", "true").lower() == "true"
# Filesystem path to a VOSK model directory (e.g. vosk-model-small-es-0.42).
# Required when WAKE_WORD_ENABLED=true. Empty string disables the feature
# even if the flag is on (and we fall back to the legacy TranscriberSink).
VOSK_MODEL_PATH = os.getenv(
    "VOSK_MODEL_PATH",
    "/home/ubuntu/vapls-discord-bot/models/vosk-model-small-es-0.42",
)
# Audio kept in a per-user circular buffer BEFORE the wake word fires. When
# VOSK detects "indio", this pre-roll is prepended to the captured chunk so
# Whisper sees the full phrase even if the user said "che indio contame…"
# all in one breath without a pause. The prebuffer is RESET whenever the
# speaker has been silent for WAKE_WORD_SILENCE_FINAL_SECONDS, so it only
# ever contains the current utterance — never audio from previous sentences.
# Keep this small enough that even if the reset misses an edge case, only
# a brief lead-in slips through.
WAKE_WORD_PREBUFFER_SECONDS = float(os.getenv("WAKE_WORD_PREBUFFER_SECONDS", "1.5"))
# Hard upper bound on how long we keep capturing audio after the wake word.
# Protects against runaway buffers if silence detection misfires.
WAKE_WORD_MAX_CAPTURE_SECONDS = float(os.getenv("WAKE_WORD_MAX_CAPTURE_SECONDS", "12.0"))
# Sustained silence inside a capture that closes it. Keep it close to the
# regular VAD final-silence threshold (~0.8s) so users feel a natural cutoff.
WAKE_WORD_SILENCE_FINAL_SECONDS = float(os.getenv("WAKE_WORD_SILENCE_FINAL_SECONDS", "0.8"))
# Number of alternative transcriptions VOSK returns for each finalized
# segment (N-best decoding). Higher = better recall on borderline pronunciations
# — if the speaker says "indio dale" but VOSK ranks "indio" #1 and "indio dale"
# #2, we accept either. 0 = single-best (legacy behavior).
VOSK_MAX_ALTERNATIVES = int(os.getenv("VOSK_MAX_ALTERNATIVES", "5"))
# Debug switch: when true the userbot falls back to the legacy TranscriberSink
# (transcribes EVERY utterance, posts every line to the transcript channel).
# Useful for visualizing what Whisper hears in the channel while debugging.
DEBUG_TRANSCRIBE_ALL = os.getenv("DEBUG_TRANSCRIBE_ALL", "false").lower() == "true"

# Main bot API (apiServer). Used to (a) check is_playing for throttling,
# (b) POST /indio when a wake-word is heard. SECRET must match the main bot's
# API_SECRET, otherwise both calls are skipped silently.
MAIN_BOT_API_BASE = os.getenv("MAIN_BOT_API_BASE", "http://127.0.0.1:8080")
MAIN_BOT_API_SECRET = os.getenv("MAIN_BOT_API_SECRET", "")

# Main bot's HTTP API base (apiServer.py). Used to ship transcripts back
# when ENABLE_HTTP_FORWARD is true.
BOT_API_BASE = os.getenv("BOT_API_BASE", "http://127.0.0.1:8080")
BOT_API_SECRET = os.getenv("BOT_API_SECRET", "")
ENABLE_HTTP_FORWARD = os.getenv("ENABLE_HTTP_FORWARD", "false").lower() == "true"

# Optional restriction: only join voice channels in these guild IDs. Empty
# = listen everywhere the user account is a member of.
_guild_ids_raw = os.getenv("GUILD_ALLOWLIST", "")
GUILD_ALLOWLIST = (
    {int(x) for x in _guild_ids_raw.split(",") if x.strip()}
    if _guild_ids_raw
    else None
)

# Comma-separated user IDs to ignore (e.g. the main bot, other bots).
_ignore_raw = os.getenv("IGNORE_USER_IDS", "")
IGNORE_USER_IDS = {int(x) for x in _ignore_raw.split(",") if x.strip()}

# Channel where text transcripts get posted, if writable by the user
# account. TRANSCRIPT_CHANNEL_ID gana sobre TRANSCRIPT_CHANNEL_NAME cuando
# está seteado — el ID sobrevive renombres del canal en Discord, el name
# se rompe. Empty / 0 en ambos = no posting (logs only).
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID", "0"))
TRANSCRIPT_CHANNEL_NAME = os.getenv("TRANSCRIPT_CHANNEL_NAME", "")

# Local HTTP relay so the main bot can ask the userbot to post a message
# as the real user account (used by /indio so replies look like they come
# from "el indio" instead of vapls). Empty secret disables the endpoint.
RELAY_HOST = os.getenv("RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.getenv("RELAY_PORT", "8081"))
RELAY_SECRET = os.getenv("RELAY_SECRET", "")
# Hard timeout para la resolución de application_commands en el canal antes
# de invocar /play o /soundpad. discord.py-self puede quedarse colgado bajo
# rate-limit o un fetch lento de cache; sin esto el HTTP del main bot se
# queda esperando sin poder cancelar. El nombre matchea el del main bot
# (config.INDIO_RELAY_TIMEOUT) — el caller pasa el mismo valor para que el
# timeout end-to-end del relay sea consistente.
INDIO_RELAY_TIMEOUT = float(os.getenv("INDIO_RELAY_TIMEOUT", "10"))

# Bot user ID of the main VaPls bot. Used to disambiguate /play and /soundpad
# slash commands when other bots in the guild expose commands with the same
# name (e.g. legacy music bots). For Discord bots the application_id equals
# the bot user_id, so this single value filters both. Default is the VaPls
# production bot ID.
VAPLS_BOT_ID = int(os.getenv("VAPLS_BOT_ID", "1489830543074918482"))

# --- Voice recording (responses to Telegram audio) -------------------------
# The main bot can POST /record to ask the userbot to capture up to N seconds
# of mixed PCM from the voice channel and POST it back to a callback URL
# (typically the Telegram bridge). The recording is encoded to OGG/Opus.

# Hard upper bound on recording duration (seconds). The /record request may
# ask for less, but never more.
RECORD_MAX_SECONDS = float(os.getenv("RECORD_MAX_SECONDS", "20"))

# RMS threshold over 16-bit PCM samples below which a frame is treated as
# silence for voice-activity detection and trailing-silence trimming.
RECORD_RMS_THRESHOLD = int(os.getenv("RECORD_RMS_THRESHOLD", "250"))

# Minimum length in seconds of recorded audio to send back. If the trimmed
# recording is shorter than this, we treat it as "no one spoke" and skip the
# callback so the Telegram bridge doesn't reply with a tiny click.
RECORD_MIN_SECONDS = float(os.getenv("RECORD_MIN_SECONDS", "0.6"))

# --- Auto-reply when "indio" is mentioned in text channels -----------------
# The userbot watches every text channel it can read and forwards the message
# to the main bot's /indio endpoint when the word "indio" appears. Throttled
# per-channel to avoid burning the Gemini free tier on chatty conversations.

# Master toggle. Defaults to false so the feature is opt-in.
INDIO_AUTO_REPLY_ENABLED = os.getenv("INDIO_AUTO_REPLY_ENABLED", "false").lower() == "true"

# Per-channel cooldown in seconds: ignore further matches in the same channel
# for this long after firing once.
INDIO_AUTO_REPLY_COOLDOWN_SEC = float(os.getenv("INDIO_AUTO_REPLY_COOLDOWN_SEC", "3"))

# Per-guild hourly cap to keep us safely under the Gemini free-tier ceiling
# (250 RPD shared across /indio slash, voice wake word, and auto-reply).
INDIO_AUTO_REPLY_GUILD_HOURLY_CAP = int(os.getenv("INDIO_AUTO_REPLY_GUILD_HOURLY_CAP", "30"))

# Seconds without any human present in any voice channel of a guild before
# the userbot disconnects. The timer is cancelled the moment a human
# (re)joins any channel of the guild. Set to 0 for the legacy "disconnect
# immediately" behaviour.
IDLE_LEAVE_SECONDS = float(os.getenv("IDLE_LEAVE_SECONDS", "60"))

# --- Greetings (sound on user join) ---------------------------------------
# Cuando un humano entra a un canal de voz donde el userbot esta presente,
# si tiene un audio especifico definido en users.py (campo "greeting") lo
# reproducimos. NO hay default: usuarios sin "greeting" no gatillan nada.
# Path absoluto (o relativo al working dir) donde viven los audios; tipicamente
# coincide con el CUSTOM_AUDIO_PATH del main bot (lo populan lsyncd + el repo).
CUSTOM_AUDIO_PATH = os.getenv(
    "CUSTOM_AUDIO_PATH",
    "/home/ubuntu/vapls-discord-bot/audio_output",
)
# Toggle maestro — false desactiva el greeting completo.
GREETING_ENABLED = os.getenv("GREETING_ENABLED", "true").lower() == "true"
# Throttle por canal: minimo de segundos entre dos greetings en el mismo VC.
GREETING_THROTTLE_SECONDS = float(os.getenv("GREETING_THROTTLE_SECONDS", "15"))

# --- Wake sound (confirmation cue on wake-word detection) ------------------
# Cuando VOSK detecta la palabra clave ("indio"), el userbot reproduce un
# sonidito corto en el canal de voz como feedback inmediato para que el
# usuario sepa que se lo escuchó. Se dispara en el momento de la detección
# (antes de validar con Whisper), así que falsos positivos también suenan;
# el throttle limita la cantidad.
WAKE_SOUND_ENABLED = os.getenv("WAKE_SOUND_ENABLED", "true").lower() == "true"
# Path al audio. Si es relativo se resuelve contra CUSTOM_AUDIO_PATH. Vacío
# = feature inactivo aunque WAKE_SOUND_ENABLED esté en true.
WAKE_SOUND_PATH = os.getenv("WAKE_SOUND_PATH", "")
# Mínimo de segundos entre dos sonidos en el mismo canal. Default 0 = sin
# throttle (cada detección suena), útil mientras se calibra la wake word.
# Subir si en producción molesta el spam.
WAKE_SOUND_THROTTLE_SECONDS = float(os.getenv("WAKE_SOUND_THROTTLE_SECONDS", "0.0"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
