"""PostHog product analytics wrapper.

If POSTHOG_API_KEY is unset, every call is a no-op so the bot still works
without a PostHog connection. Depends on config.py and the optional posthog
SDK.
"""
import logging
from typing import Any, Optional

import config

logger = logging.getLogger("bot.analytics")

_client = None
_known_groups: set[str] = set()

try:
    if config.POSTHOG_API_KEY:
        from posthog import Posthog

        _client = Posthog(
            project_api_key=config.POSTHOG_API_KEY,
            host=config.POSTHOG_HOST,
            enable_exception_autocapture=True,
        )
        logger.info("PostHog analytics initialized (host=%s)", config.POSTHOG_HOST)
    else:
        logger.info("PostHog API key not set; analytics disabled.")
except Exception as e:
    logger.warning("Failed to initialize PostHog: %s", e)
    _client = None


def _distinct_id(user) -> Optional[str]:
    """Extract a PostHog distinct_id from a Discord user.

    Args:
        user: Discord user/member object.

    Returns:
        The user ID as a string, or None if unavailable.
    """
    if user is None:
        return None
    uid = getattr(user, "id", None)
    return str(uid) if uid is not None else None


def identify_user(user) -> None:
    """Register/refresh a Discord user profile in PostHog.

    Args:
        user: Discord user/member object.

    Returns:
        None.

    Side Effects:
        Sends identify/set calls to PostHog when enabled.
    """
    if _client is None or user is None:
        return
    distinct_id = _distinct_id(user)
    if not distinct_id:
        return
    try:
        properties = {
            "discord_username": getattr(user, "name", None),
            "discord_global_name": getattr(user, "global_name", None),
            "is_bot": bool(getattr(user, "bot", False)),
        }
        _client.set(distinct_id=distinct_id, properties={k: v for k, v in properties.items() if v is not None})
    except Exception as e:
        logger.debug("identify_user failed: %s", e)


def identify_guild(guild) -> None:
    """Register the Discord guild as a PostHog group (once per process).

    Args:
        guild: Discord guild object.

    Returns:
        None.

    Side Effects:
        Sends group_identify calls to PostHog when enabled.
    """
    if _client is None or guild is None:
        return
    guild_id = str(getattr(guild, "id", "") or "")
    if not guild_id or guild_id in _known_groups:
        return
    try:
        _client.group_identify(
            group_type="guild",
            group_key=guild_id,
            properties={
                "name": getattr(guild, "name", None),
                "member_count": getattr(guild, "member_count", None),
            },
        )
        _known_groups.add(guild_id)
    except Exception as e:
        logger.debug("identify_guild failed: %s", e)


def capture(
    event: str,
    *,
    user=None,
    guild=None,
    properties: Optional[dict[str, Any]] = None,
    distinct_id: Optional[str] = None,
) -> None:
    """Capture a product analytics event.

    Args:
        event: Event name.
        user: Optional Discord user/member for distinct_id.
        guild: Optional guild for group attribution.
        properties: Additional event properties.
        distinct_id: Optional explicit distinct_id override.

    Returns:
        None.

    Side Effects:
        Emits a PostHog event when analytics are enabled.
    """
    if _client is None:
        return
    did = distinct_id or _distinct_id(user)
    props: dict[str, Any] = dict(properties or {})
    if guild is not None:
        props.setdefault("guild_id", str(getattr(guild, "id", "") or "") or None)
        props.setdefault("guild_name", getattr(guild, "name", None))
        identify_guild(guild)
    groups = None
    guild_id = props.get("guild_id")
    if guild_id:
        groups = {"guild": str(guild_id)}
    try:
        if did:
            _client.capture(event=event, distinct_id=did, properties=props, groups=groups)
        else:
            # Personless event (auto-generated distinct_id, no person profile)
            props["$process_person_profile"] = False
            _client.capture(event=event, distinct_id=f"bot-{guild_id or 'system'}", properties=props, groups=groups)
    except Exception as e:
        logger.debug("capture(%s) failed: %s", event, e)


def capture_exception(exc: BaseException, *, user=None, guild=None, properties: Optional[dict[str, Any]] = None) -> None:
    """Report an exception to PostHog.

    Args:
        exc: Exception instance to report.
        user: Optional Discord user/member context.
        guild: Optional Discord guild context.
        properties: Additional metadata to attach.

    Returns:
        None.

    Side Effects:
        Sends exception telemetry to PostHog when enabled.
    """
    if _client is None:
        return
    did = _distinct_id(user) or (f"bot-{getattr(guild, 'id', 'system')}")
    props = dict(properties or {})
    if guild is not None:
        props.setdefault("guild_id", str(getattr(guild, "id", "") or "") or None)
    try:
        _client.capture_exception(exc, distinct_id=did, properties=props)
    except Exception as e:
        logger.debug("capture_exception failed: %s", e)


def shutdown() -> None:
    """Flush and close the PostHog client if it is active."""
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception:
        pass
