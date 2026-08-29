import os
from dotenv import load_dotenv

parent_env = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(parent_env, override=True)
load_dotenv(override=True)

USER_TOKEN = os.getenv("GOLIVE_TOKEN")

RELAY_HOST = os.getenv("GOLIVE_RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.getenv("GOLIVE_RELAY_PORT", "8082"))
_raw_secret = (os.getenv("GOLIVE_RELAY_SECRET") or "").strip() or (os.getenv("RELAY_SECRET") or "").strip()
if not _raw_secret:
    # Fallback directly to parent .env if local env gave empty string
    import dotenv
    _parent_vals = dotenv.dotenv_values(parent_env)
    _raw_secret = (_parent_vals.get("GOLIVE_RELAY_SECRET") or _parent_vals.get("RELAY_SECRET") or "").strip()

RELAY_SECRET = _raw_secret or "vapls-golive-shared-secret"

_guild_raw = os.getenv("GOLIVE_GUILD_ALLOWLIST", "")
GUILD_ALLOWLIST = (
    {int(x) for x in _guild_raw.split(",") if x.strip()} if _guild_raw else None
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
