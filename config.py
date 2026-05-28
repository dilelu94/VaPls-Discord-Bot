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
