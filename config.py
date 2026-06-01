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
    DEBUG_GUILD_IDS = [int(x) for x in _guild_ids_raw.split(',') if x.strip()]
else:
    DEBUG_GUILD_IDS = None
RAM_THRESHOLD_MB = int(os.getenv("RAM_THRESHOLD_MB", "300"))  # default 300 MiB
PLAY_COOLDOWN = float(os.getenv("PLAY_COOLDOWN", "5"))  # seconds

# PostHog product analytics
POSTHOG_API_KEY = os.getenv("POSTHOG_API_KEY")
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

# HTTP API for telegram bridge
API_HOST = os.getenv("API_HOST", "127.0.0.1")
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

# Userbot relay: where the userbot exposes its POST /say endpoint so /indio
# replies can be posted by the real user account instead of the vapls bot.
# Empty INDIO_RELAY_URL disables relay (indio falls back to posting as vapls).
INDIO_RELAY_URL = os.getenv("INDIO_RELAY_URL", "")
INDIO_RELAY_SECRET = os.getenv("INDIO_RELAY_SECRET", "")
INDIO_RELAY_TIMEOUT = float(os.getenv("INDIO_RELAY_TIMEOUT", "10"))

# Cuando el indio decide poner musica via [PLAY_MUSIC: ...], los mensajes
# de estado y el panel de control del GuildPlayer se postean siempre en
# este text channel. Sin fallback: si no esta, la accion falla.
INDIO_PLAY_CHANNEL_ID = int(os.getenv("INDIO_PLAY_CHANNEL_ID", "451607097432604672"))

# Canal unico donde se postean las respuestas del Indio (sin importar desde
# donde se lo invoque: /indio, wake-word de texto, wake-word de voz, HTTP).
# 0 = comportamiento clasico (responde en el canal del trigger).
INDIO_REPLY_CHANNEL_ID = int(os.getenv("INDIO_REPLY_CHANNEL_ID", "1490008278275461280"))

# Userbot voice-recording endpoint. After /play-audio finishes playing a
# Telegram-uploaded clip we ask the userbot to capture the voice channel's
# reply and POST it back to the Telegram bridge. Leave USERBOT_RECORD_URL
# empty to disable the feature entirely. USERBOT_RECORD_SECRET typically
# matches the Telegram bridge's CALLBACK_SECRET so the same secret flows
# end-to-end; generate a separate one if you prefer to split the trust
# zones (main bot ↔ userbot vs userbot ↔ Telegram bridge).
USERBOT_RECORD_URL = os.getenv("USERBOT_RECORD_URL", "").strip()
USERBOT_RECORD_SECRET = os.getenv("USERBOT_RECORD_SECRET", "").strip()
USERBOT_RECORD_DEFAULT_DURATION = int(os.getenv("USERBOT_RECORD_DEFAULT_DURATION", "20"))
USERBOT_RECORD_TRIGGER_TIMEOUT = float(os.getenv("USERBOT_RECORD_TRIGGER_TIMEOUT", "5"))

# Cuántos segundos de inactividad (ni reproduciendo ni pausado) tolera el bot
# antes de desconectarse solo del canal de voz. Lo maneja idleWatchdog.py.
VOICE_IDLE_TIMEOUT_SECONDS = float(os.getenv("VOICE_IDLE_TIMEOUT_SECONDS", "1"))

# /sugerencias: archivo JSON donde se guardan las ideas/feature-requests de los
# usuarios. Gemini Flash-Lite agrupa ideas similares para no duplicar entradas.
SUGGESTIONS_PATH = os.getenv("SUGGESTIONS_PATH", "data/suggestions.json")
SUGGESTIONS_MODEL = os.getenv("SUGGESTIONS_MODEL", "gemini-2.5-flash-lite")

# --- ASR-quality feedback (inline reactions) -------------------------------
# Cada transcripción de voz que entra al `/indio` puede ser sampleada para
# pedir feedback de calidad del ASR: el bot agrega 👍 / ❌ al mensaje de
# transcripción. 👍 = entendió bien (no se loggea nada). ❌ = wake-word
# falso positivo o transcripción mala (se loggea a un JSONL para debug
# offline del ASR).
DECIFRAR_FEEDBACK_ENABLED = os.getenv("DECIFRAR_FEEDBACK_ENABLED", "true").lower() == "true"
# 1 de cada N transcripciones de voz recibe el par de reacciones.
DECIFRAR_FEEDBACK_SAMPLE_RATE = int(os.getenv("DECIFRAR_FEEDBACK_SAMPLE_RATE", "3"))
# Minutos antes de que el sweeper limpie las reacciones de un sample que
# nadie votó.
DECIFRAR_FEEDBACK_TIMEOUT_MINUTES = float(os.getenv("DECIFRAR_FEEDBACK_TIMEOUT_MINUTES", "60"))
DECIFRAR_FALSE_POSITIVES_LOG_PATH = os.getenv("DECIFRAR_FALSE_POSITIVES_LOG_PATH", "data/false_positives.jsonl")
