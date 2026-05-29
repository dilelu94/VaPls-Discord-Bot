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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
INDIO_MEMORY_PATH = os.getenv("INDIO_MEMORY_PATH", "data/indio_memory.json")

# Userbot relay: where the userbot exposes its POST /say endpoint so /indio
# replies can be posted by the real user account instead of the vapls bot.
# Empty INDIO_RELAY_URL disables relay (indio falls back to posting as vapls).
INDIO_RELAY_URL = os.getenv("INDIO_RELAY_URL", "")
INDIO_RELAY_SECRET = os.getenv("INDIO_RELAY_SECRET", "")
INDIO_RELAY_TIMEOUT = float(os.getenv("INDIO_RELAY_TIMEOUT", "10"))
