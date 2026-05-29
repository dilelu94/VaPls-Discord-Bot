import os
from dotenv import load_dotenv

load_dotenv()

# Discord user account token (NOT a bot token). Obtain from DevTools →
# Network → any request → headers → authorization. Treat as a secret.
USER_TOKEN = os.getenv("USER_TOKEN")

# Path to the Spanish VOSK model. Defaults to the model directory of the
# main bot so we share the same files.
MODEL_PATH_ES = os.getenv(
    "MODEL_PATH_ES",
    "/home/ubuntu/vapls-discord-bot/models/vosk-model-small-es-0.42",
)

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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
