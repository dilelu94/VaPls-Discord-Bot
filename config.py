"""Runtime configuration for the main Discord bot.

Values are loaded from environment variables (optionally via .env) at import
time and shared across modules such as bot.py, playCommand.py, soundpadCommand.py,
apiServer.py, analytics.py, and geminiClient.py.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

TOKEN = os.getenv("TOKEN")
MODEL_PATH_ES = os.getenv("MODEL_PATH_ES", "models/vosk-model-small-es-0.42")
MODEL_PATH_EN = os.getenv("MODEL_PATH_EN", "models/vosk-model-small-en-us-0.15")
AUDIO_DIR = os.getenv("AUDIO_DIR", "/var/home/dilelu/Desktop/Output")
CUSTOM_AUDIO_PATH = os.getenv("CUSTOM_AUDIO_PATH", "/var/home/dilelu/Desktop/Output")
YT_DLP_PATH = os.getenv("YT_DLP_PATH", "yt-dlp")
YT_DLP_POT_BASE_URL = os.getenv("YT_DLP_POT_BASE_URL", "http://127.0.0.1:4416")

# Guild IDs where slash commands are registered instantly (dev mode).
# Leave empty or unset to register commands globally (may take up to 1h to propagate).
_guild_ids_raw = os.getenv("DEBUG_GUILD_IDS", "")
if _guild_ids_raw:
    DEBUG_GUILD_IDS = [int(x) for x in _guild_ids_raw.split(",") if x.strip()]
else:
    DEBUG_GUILD_IDS = None
RAM_THRESHOLD_MB = int(os.getenv("RAM_THRESHOLD_MB", "300"))  # default 300 MiB
PLAY_COOLDOWN = float(os.getenv("PLAY_COOLDOWN", "5"))  # seconds

# Discord user ID of the bot owner. Used to gate owner-only commands like
# /actividad for the MMR ranking system.
OWNER_ID = int(os.getenv("OWNER_ID", "211354006805676032"))

# Discord user ID of the userbot (Indio). Excluded from occupancy counts
# so its presence doesn't trigger or extend MMR activities meant for ≥2 humans.
USERBOT_USER_ID = int(os.getenv("USERBOT_USER_ID", "0"))

# PostHog product analytics
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

# HTTP API for telegram bridge
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8080"))
API_SECRET = os.getenv("API_SECRET", "")


# Google Gemini API (https://aistudio.google.com/apikey) - tier gratuito
# Soporta una sola key (GEMINI_API_KEY) o un pool comma-separated
# (GEMINI_API_KEYS) que el cliente rota con failover en HTTP 429.
def _parse_gemini_keys() -> list[str]:
    multi = os.getenv("GEMINI_API_KEYS", "")
    if multi:
        return [k.strip() for k in multi.split(",") if k.strip()]
    single = os.getenv("GEMINI_API_KEY", "").strip()
    return [single] if single else []


GEMINI_API_KEYS: list[str] = _parse_gemini_keys()
# Back-compat: many call sites still read GEMINI_API_KEY as the "is configured?"
# truthy check; mantenelo apuntando a la primera key del pool.
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else None
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Archivo persistente con el pool de keys (gitignored). geminiKeys.py lo lee
# al startup y lo escribe cuando alguien manda una key nueva por DM. Si no
# existe, se siembra con GEMINI_API_KEYS del .env.
GEMINI_KEYS_FILE = os.getenv("GEMINI_KEYS_FILE", "gemini_keys.json")
GEMINI_KEYS_DONATION_URL = "https://aistudio.google.com/apikey"
INDIO_MEMORY_PATH = os.getenv("INDIO_MEMORY_PATH", "data/indio_memory.json")
INDIO_IMAGES_DIR = os.getenv("INDIO_IMAGES_DIR", "indio_images")
INDIO_IMAGE_GUILD_ID = int(os.getenv("INDIO_IMAGE_GUILD_ID", "0"))

# Ruta al archivo JSON con datos persistidos de mascotas (/mascota).
PETS_PATH = os.getenv("PETS_PATH", "data/pets.json")
PET_GENERATION_COST = int(os.getenv("PET_GENERATION_COST", "300"))
PET_EVOLUTION_COST = int(os.getenv("PET_EVOLUTION_COST", "300"))

# Ruta al archivo JSON con datos estáticos de usuarios (traits, anécdotas, etc.).
# Default: data/users.json. Si no existe, se usa el fallback hardcodeado en users.py.
USERS_PATH = os.getenv("USERS_PATH", "data/users.json")

# MMR admin page Basic Auth credentials. Fall back to defaults for
# backward compatibility but SHOULD be overridden in production.
ADMIN_USER = os.getenv("ADMIN_USER", "dilelu")
ADMIN_PASS = os.getenv("ADMIN_PASS", "indiovapls")

# Userbot relay: where the userbot exposes its POST /say endpoint so /indio
# replies can be posted by the real user account instead of the vapls bot.
# Empty INDIO_RELAY_URL disables relay (indio falls back to posting as vapls).
INDIO_RELAY_URL = os.getenv("INDIO_RELAY_URL", "")
INDIO_RELAY_SECRET = os.getenv("INDIO_RELAY_SECRET", "")
INDIO_RELAY_TIMEOUT = float(os.getenv("INDIO_RELAY_TIMEOUT", "10"))

# GoLive userbot relay: separate user account for IPTV Go Live streaming.
# The /stream slash command POSTs here instead of the indio relay.
GOLIVE_RELAY_URL = os.getenv("GOLIVE_RELAY_URL", "")
GOLIVE_RELAY_SECRET = os.getenv("GOLIVE_RELAY_SECRET", "")
GOLIVE_RELAY_TIMEOUT = float(os.getenv("GOLIVE_RELAY_TIMEOUT", "10"))

# Cuando el indio decide poner musica via [PLAY_MUSIC: ...], los mensajes
# de estado y el panel de control del GuildPlayer se postean siempre en
# este text channel. Si esta en 0, el relay /play falla explicitamente y
# la accion cae al playFromIndio local (que tiene su propio canal-pick).
INDIO_PLAY_CHANNEL_ID = int(os.getenv("INDIO_PLAY_CHANNEL_ID", "451607097432604672"))

# Canal unico donde se postean las respuestas del Indio para el path de
# TEXTO: /indio slash command y wake-word de texto. La wake-word de VOZ
# queda exenta (responde en el canal del transcript, sin redirect/header/DM)
# — apiServer.indioVoice propaga is_voice=True hasta indioFromVoice, que
# saltea este override cuando from_voice=True.
# 0 = comportamiento clasico (responde en el canal del trigger).
INDIO_REPLY_CHANNEL_ID = int(os.getenv("INDIO_REPLY_CHANNEL_ID", "1490008278275461280"))

# --- Indio story system ----------------------------------------------------
# Canal donde se postean las historias generadas para revisión comunitaria.
INDIO_STORY_CHANNEL_ID = int(os.getenv("INDIO_STORY_CHANNEL_ID", "451580655650996236"))
# Directorio donde se extraen las imágenes del pool.
INDIO_STORY_POOL_DIR = os.getenv("INDIO_STORY_POOL_DIR", "indio_images/pool")
# Máximo de stories por día (por servidor).
INDIO_MAX_STORIES_PER_DAY = int(os.getenv("INDIO_MAX_STORIES_PER_DAY", "2"))
# Minutos de inactividad en el chat para trigger idle.
INDIO_IDLE_MINUTES = int(os.getenv("INDIO_IDLE_MINUTES", "240"))
# Delay mínimo/máximo (segundos) entre detección idle y posteo real.
INDIO_STORY_IDLE_DELAY_MIN = int(os.getenv("INDIO_STORY_IDLE_DELAY_MIN", "3600"))  # 1h
INDIO_STORY_IDLE_DELAY_MAX = int(os.getenv("INDIO_STORY_IDLE_DELAY_MAX", "7200"))  # 2h
# Mínimo de mensajes después del último story para permitir otro.
INDIO_STORY_MIN_MESSAGES_AFTER = int(os.getenv("INDIO_STORY_MIN_MESSAGES_AFTER", "5"))
# Mínimo de humanos en un canal de voz para trigger voice.
INDIO_STORY_VOICE_MIN_MEMBERS = int(os.getenv("INDIO_STORY_VOICE_MIN_MEMBERS", "3"))
# Minutos de inactividad para forzar la 1ra historia del día (min 1/día).
INDIO_STORY_DAILY_MIN_IDLE = int(os.getenv("INDIO_STORY_DAILY_MIN_IDLE", "60"))

# Hugging Face Inference API for /generarimagen (free tier, no API key needed
# for inference, just a Hugging Face token). Sign up at huggingface.co and
# create a read token at https://huggingface.co/settings/tokens.
# Leave HUGGINGFACE_API_TOKEN empty to disable image generation.
HUGGINGFACE_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN", "").strip()

# Cloudflare Workers AI for free image-to-image / image-editing.
# Sign up for a free account at cloudflare.com (no credit card required for the free tier).
# Create an API token with "Workers AI" permissions and find your Account ID in the dashboard.
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()

# Canales donde el bot puede postear mensajes publicos. En cualquier otro canal
# las respuestas de /vapls salen ephemeral (solo las ve el invocador), para no
# ensuciar canales que no son los del bot.
PUBLIC_ALLOWED_CHANNEL_IDS = {
    cid
    for cid in (INDIO_PLAY_CHANNEL_ID, INDIO_REPLY_CHANNEL_ID, INDIO_STORY_CHANNEL_ID)
    if cid
}

# Userbot voice-recording endpoint. After /play-audio finishes playing a
# Telegram-uploaded clip we ask the userbot to capture the voice channel's
# reply and POST it back to the Telegram bridge. Leave USERBOT_RECORD_URL
# empty to disable the feature entirely. USERBOT_RECORD_SECRET typically
# matches the Telegram bridge's CALLBACK_SECRET so the same secret flows
# end-to-end; generate a separate one if you prefer to split the trust
# zones (main bot ↔ userbot vs userbot ↔ Telegram bridge).
USERBOT_RECORD_URL = os.getenv("USERBOT_RECORD_URL", "").strip()
USERBOT_RECORD_SECRET = os.getenv("USERBOT_RECORD_SECRET", "").strip()
USERBOT_RECORD_DEFAULT_DURATION = int(
    os.getenv("USERBOT_RECORD_DEFAULT_DURATION", "20")
)
USERBOT_RECORD_TRIGGER_TIMEOUT = float(os.getenv("USERBOT_RECORD_TRIGGER_TIMEOUT", "5"))

# Cuántos segundos de inactividad (ni reproduciendo ni pausado) tolera el bot
# antes de desconectarse solo del canal de voz. Lo maneja idleWatchdog.py.
VOICE_IDLE_TIMEOUT_SECONDS = float(os.getenv("VOICE_IDLE_TIMEOUT_SECONDS", "1"))

# /sugerencias: archivo JSON donde se guardan las ideas/feature-requests de los
# usuarios. Gemini Flash-Lite agrupa ideas similares para no duplicar entradas.
SUGGESTIONS_PATH = os.getenv("SUGGESTIONS_PATH", "data/suggestions.json")
SUGGESTIONS_MODEL = os.getenv("SUGGESTIONS_MODEL", "gemini-2.5-flash-lite")

# GitHub Issues for /sugerencias: when GITHUB_TOKEN is set, each suggestion
# group becomes a GitHub issue with label "sugerencia". Matched suggestions
# add +1 comments. Leave GITHUB_TOKEN empty for local-only.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "dilelu94/VaPls-Discord-Bot").strip()
GITHUB_ISSUE_LABEL = os.getenv("GITHUB_ISSUE_LABEL", "sugerencia").strip()

# --- File transfer (/transferir) --------------------------------------------
# Directory where uploaded files and metadata are stored.
TRANSFER_DIR = os.getenv("TRANSFER_DIR", "transfers")
# Hard limit per file in bytes (15 GB).
TRANSFER_MAX_SIZE = int(os.getenv("TRANSFER_MAX_SIZE", str(15 * 1024**3)))
# Default per-file limit shown to the user (10 GB).
TRANSFER_DEFAULT_LIMIT = int(os.getenv("TRANSFER_DEFAULT_LIMIT", str(10 * 1024**3)))
# Session TTL in seconds (5 min) — resets on each chunk upload.
TRANSFER_SESSION_TTL = int(os.getenv("TRANSFER_SESSION_TTL", "300"))
# How many hours a completed file stays alive before auto-delete.
TRANSFER_EXPIRY_HOURS = float(os.getenv("TRANSFER_EXPIRY_HOURS", "24"))
# Chunk size in bytes for resumable upload (10 MB).
TRANSFER_CHUNK_SIZE = int(os.getenv("TRANSFER_CHUNK_SIZE", str(10 * 1024**2)))
# Discord role required to use /transferir.
TRANSFER_REQUIRED_ROLE = os.getenv("TRANSFER_REQUIRED_ROLE", "Main Characters")
# Minimum free disk bytes before rejecting new uploads (5 GB).
TRANSFER_DISK_RESERVE = int(os.getenv("TRANSFER_DISK_RESERVE", str(5 * 1024**3)))
# History log path (permanent record of all uploads).
TRANSFER_HISTORY_PATH = os.getenv("TRANSFER_HISTORY_PATH", "transfers/_history.jsonl")
# External base URL for download links (no trailing slash).
TRANSFER_BASE_URL = os.getenv("TRANSFER_BASE_URL", "http://141.148.84.55")
# Sweeper interval in seconds (10 min).
TRANSFER_SWEEPER_INTERVAL = int(os.getenv("TRANSFER_SWEEPER_INTERVAL", "600"))

# --- ASR-quality feedback (inline reactions) -------------------------------
# Cada transcripción de voz que entra al `/indio` puede ser sampleada para
# pedir feedback de calidad del ASR: el bot agrega 👍 / ❌ al mensaje de
# transcripción. 👍 = entendió bien (no se loggea nada). ❌ = wake-word
# falso positivo o transcripción mala (se loggea a un JSONL para debug
# offline del ASR).
DECIFRAR_FEEDBACK_ENABLED = (
    os.getenv("DECIFRAR_FEEDBACK_ENABLED", "true").lower() == "true"
)
# 1 de cada N transcripciones de voz recibe el par de reacciones.
DECIFRAR_FEEDBACK_SAMPLE_RATE = int(os.getenv("DECIFRAR_FEEDBACK_SAMPLE_RATE", "3"))
# Minutos antes de que el sweeper limpie las reacciones de un sample que
# nadie votó.
DECIFRAR_FEEDBACK_TIMEOUT_MINUTES = float(
    os.getenv("DECIFRAR_FEEDBACK_TIMEOUT_MINUTES", "60")
)
DECIFRAR_FALSE_POSITIVES_LOG_PATH = os.getenv(
    "DECIFRAR_FALSE_POSITIVES_LOG_PATH", "data/false_positives.jsonl"
)

# Auto-DJ: when the queue empties and Auto-DJ is active, the Indio picks the
# next song from the YouTube Mix of the last track and posts a suggestion.
# Users have AUTODJ_GRACE_SECONDS to veto before it plays automatically.
# After AUTODJ_MAX_CHAIN consecutive Auto-DJ tracks the mode shuts itself off.
AUTODJ_GRACE_SECONDS = int(os.getenv("AUTODJ_GRACE_SECONDS", "15"))
AUTODJ_MAX_CHAIN = int(os.getenv("AUTODJ_MAX_CHAIN", "10"))

# Canal fijo donde el menú del modo DJ se postea siempre — tanto desde /dj
# como cuando el Indio detecta el pedido en el chat de texto. Por defecto
# coincide con INDIO_PLAY_CHANNEL_ID (el canal de música del grupo).
AUTODJ_MENU_CHANNEL_ID = int(os.getenv("AUTODJ_MENU_CHANNEL_ID", "451607097432604672"))

# --- Israel rocket/missile alerts (Tzevaadom) --------------------------------
# When enabled, the bot connects to the Tzevaadom WebSocket and posts real-time
# alerts to the configured channel. Requires a channel ID to post to.
ISRAEL_ALERTS_ENABLED = os.getenv("ISRAEL_ALERTS_ENABLED", "false").lower() == "true"
ISRAEL_ALERTS_CHANNEL_ID = int(os.getenv("ISRAEL_ALERTS_CHANNEL_ID", "0"))
