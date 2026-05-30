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
# account. Empty = no posting (logs only).
TRANSCRIPT_CHANNEL_NAME = os.getenv("TRANSCRIPT_CHANNEL_NAME", "")

# Local HTTP relay so the main bot can ask the userbot to post a message
# as the real user account (used by /indio so replies look like they come
# from "el indio" instead of vapls). Empty secret disables the endpoint.
RELAY_HOST = os.getenv("RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.getenv("RELAY_PORT", "8081"))
RELAY_SECRET = os.getenv("RELAY_SECRET", "")

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
# for this long after firing once. 180s = 3 min.
INDIO_AUTO_REPLY_COOLDOWN_SEC = float(os.getenv("INDIO_AUTO_REPLY_COOLDOWN_SEC", "180"))

# Per-guild hourly cap to keep us safely under the Gemini free-tier ceiling
# (250 RPD shared across /indio slash, voice wake word, and auto-reply).
INDIO_AUTO_REPLY_GUILD_HOURLY_CAP = int(os.getenv("INDIO_AUTO_REPLY_GUILD_HOURLY_CAP", "30"))

# Seconds without any human present in any voice channel of a guild before
# the userbot disconnects. The timer is cancelled the moment a human
# (re)joins any channel of the guild. Set to 0 for the legacy "disconnect
# immediately" behaviour.
IDLE_LEAVE_SECONDS = float(os.getenv("IDLE_LEAVE_SECONDS", "60"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
