"""PostHog product analytics wrapper.

This module delegates all tracking and identification calls directly to
`posthog_client.py`. All imports and initializations stay inside `posthog_client.py`.
"""
import logging
from typing import Any, Optional

import posthog_client

logger = logging.getLogger("bot.analytics")


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
    """
    if user is None:
        return
    did = _distinct_id(user)
    if not did:
        return
    try:
        properties = {
            "discord_username": getattr(user, "name", None),
            "discord_global_name": getattr(user, "global_name", None),
            "is_bot": bool(getattr(user, "bot", False)),
        }
        posthog_client.identify_user(
            did,
            **{k: v for k, v in properties.items() if v is not None}
        )
    except Exception as e:
        logger.debug("identify_user failed: %s", e)


def identify_guild(guild) -> None:
    """Register the Discord guild as a PostHog group (once per process).

    Args:
        guild: Discord guild object.
    """
    if guild is None:
        return
    guild_id = str(getattr(guild, "id", "") or "")
    if not guild_id:
        return
    posthog_client.group_identify(
        "guild",
        guild_id,
        name=getattr(guild, "name", None),
        member_count=getattr(guild, "member_count", None),
    )


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
    """
    did = distinct_id or _distinct_id(user)
    props = dict(properties or {})
    if guild is not None:
        props.setdefault("guild_id", str(getattr(guild, "id", "") or "") or None)
        props.setdefault("guild_name", getattr(guild, "name", None))
        identify_guild(guild)

    guild_id = props.get("guild_id")
    groups = {"guild": str(guild_id)} if guild_id else None
    # Avoid collision with keyword/positional arguments in posthog_client.track_request
    if "user_id" in props:
        props["property_user_id"] = props.pop("user_id")
    # did may be None: track_request then captures a personless event, so bot
    # and system actions never create a person profile in PostHog.
    posthog_client.track_request(did, event, groups=groups, **props)


def capture_exception(exc: BaseException, *, user=None, guild=None, properties: Optional[dict[str, Any]] = None) -> None:
    """Report an exception to PostHog.

    Args:
        exc: Exception instance to report.
        user: Optional Discord user/member context.
        guild: Optional Discord guild context.
        properties: Additional metadata to attach.
    """
    did = _distinct_id(user) or (f"bot-{getattr(guild, 'id', 'system')}")
    props = dict(properties or {})
    if guild is not None:
        props.setdefault("guild_id", str(getattr(guild, "id", "") or "") or None)

    # Avoid collision with keyword arguments in posthog_client.capture_error
    if "user_id" in props:
        props["property_user_id"] = props.pop("user_id")
    posthog_client.capture_error(exc, user_id=did, **props)


def shutdown() -> None:
    """Flush and close the PostHog client if it is active."""
    client = getattr(posthog_client, "_posthog", None)
    if client is not None:
        try:
            client.shutdown()
        except Exception:
            pass
