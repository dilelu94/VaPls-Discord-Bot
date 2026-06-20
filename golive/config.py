import os
from dotenv import load_dotenv

load_dotenv()

USER_TOKEN = os.getenv("GOLIVE_TOKEN")

RELAY_HOST = os.getenv("GOLIVE_RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.getenv("GOLIVE_RELAY_PORT", "8082"))
RELAY_SECRET = os.getenv("GOLIVE_RELAY_SECRET", "")

_guild_raw = os.getenv("GOLIVE_GUILD_ALLOWLIST", "")
GUILD_ALLOWLIST = (
    {int(x) for x in _guild_raw.split(",") if x.strip()} if _guild_raw else None
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
