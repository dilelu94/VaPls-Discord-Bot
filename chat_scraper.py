"""
Background chat scraper for VaPls Discord bot.

Safely fetches historical messages from text channels using pagination,
enforcing anti rate-limit delays and persisting progress in SQLite.
"""

import asyncio
import logging
import time
from typing import Optional
import discord
import chat_db
import config

logger = logging.getLogger(__name__)

# Delay between history pagination requests (seconds)
BATCH_DELAY_SECONDS = 1.5
# Max messages per history API call
BATCH_LIMIT = 100

_scraper_task: Optional[asyncio.Task] = None


def format_message_dict(msg: discord.Message) -> dict:
    """Format a discord.Message into a dict suitable for chat_db."""
    author = getattr(msg, "author", None)
    author_name = (
        getattr(author, "display_name", None)
        or getattr(author, "name", "alguien")
        if author
        else "alguien"
    )
    guild_id = getattr(getattr(msg, "guild", None), "id", 0)
    channel_name = getattr(getattr(msg, "channel", None), "name", "")
    created_at = (
        int(msg.created_at.timestamp())
        if hasattr(msg, "created_at") and msg.created_at
        else int(time.time())
    )

    return {
        "message_id": msg.id,
        "guild_id": guild_id,
        "channel_id": msg.channel.id,
        "channel_name": channel_name,
        "author_id": author.id if author else 0,
        "author_name": author_name,
        "content": msg.content or "",
        "has_attachments": 1 if getattr(msg, "attachments", None) else 0,
        "created_at": created_at,
    }


def should_index_message(msg: discord.Message) -> bool:
    """Determine whether a message should be indexed into chat_db."""
    if msg is None or not getattr(msg, "author", None):
        return False

    author = msg.author
    # Ignore bots/userbots
    if getattr(author, "bot", False):
        return False
    if author.id in (config.USERBOT_USER_ID, config.GOLIVE_USER_ID):
        return False

    # Ignore messages with empty text AND no attachments
    content = (msg.content or "").strip()
    has_attachments = bool(getattr(msg, "attachments", None))
    if not content and not has_attachments:
        return False

    return True


async def scrape_channel_history(channel: discord.TextChannel) -> int:
    """Scrape history for a single text channel asynchronously until 100% complete."""
    if not hasattr(channel, "history") or not hasattr(channel, "id"):
        return 0

    channel_id = channel.id
    guild_id = getattr(channel.guild, "id", 0)
    channel_name = getattr(channel, "name", str(channel_id))

    progress = chat_db.get_scrape_progress(channel_id)
    if progress and progress.get("completed"):
        logger.debug("Scrape skipped for #%s — already 100%% complete", channel_name)
        return 0

    oldest_id = progress.get("oldest_message_id") if progress else None
    total_added = 0

    logger.info("Iniciando scraping histórico para #%s (channel_id=%s)", channel_name, channel_id)

    while True:
        try:
            before_obj = discord.Object(id=oldest_id) if oldest_id else None
            messages = []
            async for m in channel.history(limit=BATCH_LIMIT, before=before_obj, oldest_first=False):
                messages.append(m)

            if not messages:
                chat_db.mark_scrape_completed(channel_id)
                logger.info("🎉 Scraping completado al 100%% para #%s (channel_id=%s)", channel_name, channel_id)
                break

            valid_dicts = [
                format_message_dict(m) for m in messages if should_index_message(m)
            ]
            added = chat_db.save_messages_batch(valid_dicts)
            total_added += added

            oldest_msg = min(messages, key=lambda m: m.id)
            oldest_id = oldest_msg.id

            chat_db.update_scrape_progress(
                channel_id=channel_id,
                guild_id=guild_id,
                channel_name=channel_name,
                oldest_message_id=oldest_id,
                added_count=added,
                completed=False,
            )

            await asyncio.sleep(BATCH_DELAY_SECONDS)

        except discord.Forbidden:
            logger.warning("Permiso denegado para scrapear #%s (channel_id=%s)", channel_name, channel_id)
            break
        except Exception as e:
            logger.warning("Error scrapeando #%s (channel_id=%s): %s", channel_name, channel_id, e)
            await asyncio.sleep(5.0)
            break

    return total_added


async def _run_scraper_loop(bot: discord.Bot) -> None:
    """Iterate through all accessible text channels and scrape history."""
    await asyncio.sleep(10.0)  # Wait for bot startup to settle
    logger.info("Iniciando ciclo de scraping de canales...")

    while True:
        try:
            guilds = getattr(bot, "guilds", [])
            for guild in guilds:
                text_channels = [
                    ch for ch in getattr(guild, "channels", [])
                    if isinstance(ch, discord.TextChannel)
                ]
                for ch in text_channels:
                    try:
                        await scrape_channel_history(ch)
                    except Exception as e:
                        logger.exception("Error insospechado scrapeando canal %s: %s", ch, e)
                    await asyncio.sleep(1.0)
        except Exception as e:
            logger.exception("Error en ciclo principal de scraping: %s", e)

        # Re-check channels every 30 minutes in case new channels or incomplete ones exist
        await asyncio.sleep(1800)


def start_background_scrape(bot: discord.Bot) -> None:
    """Start the background scraper task if not already running."""
    global _scraper_task
    if _scraper_task is None or _scraper_task.done():
        _scraper_task = asyncio.create_task(_run_scraper_loop(bot))
        logger.info("Background chat scraper task started.")
