"""Indio story system: auto-generates chistes from pool images with community review.

Triggered by voice occupancy (>2 humans) or chat idle (>4h), generates a story
via Gemini (Indio persona + image), posts to the review channel for ✅/❌/reply
feedback, and saves approved stories to the image catalog.
"""

import asyncio
import base64
import logging
import random
import time
from pathlib import Path
from typing import Optional

import discord

import config
import geminiClient
import imageManager
import imagePool

logger = logging.getLogger("bot.story")

# ── State ──────────────────────────────────────────────────────────────────
_stories_today: dict[int, int] = {}
_story_date: str = ""
_last_story_at: dict[int, float] = {}
_last_chat_activity: dict[int, float] = {}
_messages_since_story: dict[int, int] = {}
_pending_reviews: dict[int, dict] = {}
_idle_scheduled: set[int] = set()
_last_voice_trigger: dict[int, float] = {}

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


_STORY_PROMPT = """\
Sos el Indio, un amigo del grupo de Discord "VaPls". Estás viendo una imagen \
de uno de los pibes (Viny, Fox, Yo, Eyyman, Seba, Franko, Juji, Santi, Tobi, \
Mati, Fidel o del grupo Varios).

Hacé un chiste corto sobre esta imagen, como si se lo contaras al grupo de \
amigos. Tiene que ser una joda entre amigos, no una descripción de la imagen. \
Tampoco digas "esta imagen" o "en esta foto" — hablalo como si fuera algo que \
pasó o una situación que todos conocen.

Max 2-3 oraciones. En español rioplatense, con voseo. Informal, de barrio, \
como hablan los pibes. Sin comillas, sin formato, solo el chiste."""


# ── Guards ─────────────────────────────────────────────────────────────────


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _reset_daily() -> None:
    global _story_date, _stories_today
    t = _today()
    if t != _story_date:
        _stories_today.clear()
        _story_date = t


def _can_post_story(guild_id: int) -> bool:
    _reset_daily()
    cnt = _stories_today.get(guild_id, 0)
    if cnt >= config.INDIO_MAX_STORIES_PER_DAY:
        logger.info("story guard: guild %s already hit daily max (%d)", guild_id, cnt)
        return False
    pending = [r for r in _pending_reviews.values() if r.get("guild_id") == guild_id]
    if pending:
        logger.info(
            "story guard: guild %s has pending review (msg %s)",
            guild_id,
            pending[0].get("_msg_id", "?"),
        )
        return False
    last = _last_story_at.get(guild_id, 0.0)
    if last == 0.0:
        return True
    since = _messages_since_story.get(guild_id, 0)
    if since < config.INDIO_STORY_MIN_MESSAGES_AFTER:
        logger.info(
            "story guard: guild %s only %d msgs since last story (need %d)",
            guild_id,
            since,
            config.INDIO_STORY_MIN_MESSAGES_AFTER,
        )
        return False
    return True


# ── Image → Gemini ─────────────────────────────────────────────────────────


def _read_image_as_part(rel_path: str) -> Optional[dict]:
    full = Path(imagePool.POOL_DIR, rel_path)
    if not full.exists():
        return None
    raw = full.read_bytes()
    ext = full.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    return {
        "inlineData": {
            "mimeType": mime,
            "data": base64.b64encode(raw).decode(),
        }
    }


# ── Story pipeline ─────────────────────────────────────────────────────────


async def _generate_story(
    rel_path: str, user_feedback: Optional[str] = None
) -> Optional[str]:
    img_part = _read_image_as_part(rel_path)
    if img_part is None:
        logger.warning("story image not found: %s", rel_path)
        return None

    msg = "Hacé un chiste sobre esta imagen."
    if user_feedback:
        msg = (
            f"El grupo dijo: {user_feedback}\n\n"
            "Tomalo como idea y hacé otro chiste sobre la misma imagen."
        )

    try:
        reply = await geminiClient.generate(
            user_message=msg,
            system_instruction=_STORY_PROMPT,
            image_parts=[img_part],
            max_output_tokens=512,
        )
        return reply.text
    except geminiClient.GeminiError as e:
        logger.warning("story gemini error: %s", e)
        return None


async def _post_review(
    channel_id: int, rel_path: str, story_text: str, guild_id: int, bot
) -> bool:
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception:
            logger.error("review channel %d not found", channel_id)
            return False
    if not hasattr(ch, "send"):
        return False

    full = Path(imagePool.POOL_DIR, rel_path)
    file = None
    if full.exists():
        file = discord.File(str(full))

    content = (
        f"**🐔 El Indio vio una imagen y tiene algo que decir:**\n\n"
        f"{story_text}\n\n"
        f"— — —\n✅ la aprueban  ·  ❌ la rechazan  ·  "
        f"respondé con otra idea para regenerar"
    )

    try:
        msg = await ch.send(content, file=file)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
        _pending_reviews[msg.id] = {
            "_msg_id": msg.id,
            "rel_path": rel_path,
            "story_text": story_text,
            "channel_id": channel_id,
            "guild_id": guild_id,
        }
        return True
    except Exception as e:
        logger.error("post review failed: %s", e)
        return False


_DESCRIBE_PROMPT = """\
Describí esta imagen en 1-2 oraciones en español. Si hay un famoso (actor, \
cantante, deportista, político, artista, etc.) decí quién es. Luego, en una \
nueva línea, escribí "TAGS:" seguido de 3-5 tags separados por coma que \
describan la imagen (en español).

Ejemplo:
Un señor con barba y gafas oscuras, parece un árbol de Navidad andando. Es el Indio Solari.
TAGS: indio solari, redondo, arbol de navidad, recital, rock nacional"""


async def _describe_image(rel_path: str) -> tuple[str, list[str]]:
    """Call Gemini to describe the image + generate tags.

    Returns ``(description, tags)``. On failure returns ``("Imagen", ["indio_story"])``
    so the save still works.
    """
    img_part = _read_image_as_part(rel_path)
    if img_part is None:
        return "Imagen", ["indio_story"]
    try:
        reply = await geminiClient.generate(
            user_message="Describí esta imagen y dame tags.",
            system_instruction=_DESCRIBE_PROMPT,
            image_parts=[img_part],
            max_output_tokens=256,
        )
        text = reply.text
        desc, _, tags_line = text.partition("TAGS:")
        desc = desc.strip()
        tags = (
            [t.strip() for t in tags_line.split(",") if t.strip()]
            if tags_line
            else ["indio_story"]
        )
        if not desc:
            desc = "Imagen"
        if not tags:
            tags = ["indio_story"]
        logger.info("describe_image: desc=%s tags=%s", desc[:60], tags)
        return desc, tags
    except geminiClient.GeminiError as e:
        logger.warning("describe_image gemini error: %s", e)
        return "Imagen", ["indio_story"]


async def _save_approved_story(rel_path: str, story_text: str) -> Optional[str]:
    full = Path(imagePool.POOL_DIR, rel_path)
    if not full.exists():
        return None
    mgr = _init_image_mgr()
    ext = full.suffix.lstrip(".").lower()
    raw = full.read_bytes()

    desc, tags = await _describe_image(rel_path)

    img_id = mgr.add_image(
        file_bytes=raw,
        ext=ext,
        description=desc,
        tags=tags,
        author_id=0,
        original_filename=rel_path,
        gemini_description=story_text,
    )
    imagePool.remove_from_pool(rel_path)
    logger.info("story saved as image %s (was %s) desc=%s", img_id, rel_path, desc[:60])
    return img_id


_image_mgr: Optional[imageManager.ImageManager] = None


def _init_image_mgr() -> imageManager.ImageManager:
    global _image_mgr
    if _image_mgr is None:
        _image_mgr = imageManager.ImageManager(config.INDIO_IMAGES_DIR)
    return _image_mgr


# ── Public API ─────────────────────────────────────────────────────────────


async def trigger_story(
    bot, guild_id: int, channel_id: int, trigger_type: str = "idle"
) -> bool:
    logger.info("story trigger(%s) called for guild %s", trigger_type, guild_id)

    if not _can_post_story(guild_id):
        logger.info(
            "story trigger(%s): guard blocked for guild %s", trigger_type, guild_id
        )
        return False

    pool_count = await imagePool.init_pool()
    logger.info(
        "story trigger(%s): pool has %d images",
        trigger_type,
        pool_count,
    )

    mgr = _init_image_mgr()
    pick = imagePool.get_random_image(mgr)
    if pick is None:
        logger.warning(
            "story trigger(%s): no images left in pool for guild %s",
            trigger_type,
            guild_id,
        )
        return False

    rel_path = pick["rel_path"]
    logger.info("story trigger(%s): picked image %s", trigger_type, rel_path)

    story = await _generate_story(rel_path)
    if story is None:
        logger.warning(
            "story trigger(%s): gemini returned no story for %s", trigger_type, rel_path
        )
        return False

    logger.info(
        "story trigger(%s): gemini generated story (%d chars)", trigger_type, len(story)
    )

    ok = await _post_review(
        config.INDIO_STORY_CHANNEL_ID, rel_path, story, guild_id, bot
    )
    if not ok:
        logger.warning(
            "story trigger(%s): post_review failed for guild %s", trigger_type, guild_id
        )
        return False

    _reset_daily()
    _stories_today[guild_id] = _stories_today.get(guild_id, 0) + 1
    _last_story_at[guild_id] = time.time()
    _messages_since_story[guild_id] = 0
    logger.info(
        "story trigger(%s): SUCCESS for guild %s (day total: %d)",
        trigger_type,
        guild_id,
        _stories_today[guild_id],
    )
    return True


async def _clear_review_reactions(ch, mid: int) -> None:
    """Remove ✅/❌ reactions from a review message."""
    try:
        msg = await ch.fetch_message(mid)
        await msg.clear_reaction("✅")
        await msg.clear_reaction("❌")
    except Exception:
        pass


async def handle_story_reaction(payload, bot) -> None:
    mid = payload.message_id
    review = _pending_reviews.get(mid)
    if review is None:
        return
    if bot.user and payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    ch = bot.get_channel(review["channel_id"])
    if not ch or not hasattr(ch, "send"):
        _pending_reviews.pop(mid, None)
        return

    if emoji == "✅":
        img_id = await _save_approved_story(review["rel_path"], review["story_text"])
        if img_id:
            try:
                await ch.send("✅ **Aprobada! La historia se guardó.**")
            except Exception:
                pass
        _pending_reviews.pop(mid, None)
        await _clear_review_reactions(ch, mid)

    elif emoji == "❌":
        _pending_reviews.pop(mid, None)
        try:
            await ch.send("❌ **Chiste rechazado.** La imagen vuelve al pool.")
        except Exception:
            pass
        await _clear_review_reactions(ch, mid)


async def handle_story_reply(message, bot) -> None:
    ref = message.reference
    if ref is None or ref.message_id is None:
        return
    mid = ref.message_id
    review = _pending_reviews.get(mid)
    if review is None:
        return

    feedback = (message.content or "").strip()
    if not feedback:
        return

    rel_path = review["rel_path"]
    guild_id = review.get("guild_id", 0)
    new_story = await _generate_story(rel_path, user_feedback=feedback)
    if new_story is None:
        return

    _pending_reviews.pop(mid, None)
    await _post_review(review["channel_id"], rel_path, new_story, guild_id, bot)


async def start_story_watcher(bot) -> None:
    await imagePool.init_pool()
    logger.info("story watcher started")

    while True:
        try:
            await asyncio.sleep(60)
            await _watch_loop(bot)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("story watcher error")


async def _watch_loop(bot) -> None:
    for guild in bot.guilds:
        gid = guild.id
        if not _can_post_story(gid):
            continue

        last_activity = _last_chat_activity.get(gid, 0.0)
        if last_activity == 0.0:
            continue
        idle_secs = time.time() - last_activity
        idle_min = config.INDIO_IDLE_MINUTES * 60
        daily_min = config.INDIO_STORY_DAILY_MIN_IDLE * 60
        has_story_today = _stories_today.get(gid, 0) > 0

        # Min 1/day: if no story today and idle > daily_min, trigger now
        if not has_story_today and idle_secs >= daily_min:
            _spawn(
                trigger_story(
                    bot, gid, config.INDIO_STORY_CHANNEL_ID, trigger_type="daily_min"
                )
            )
            continue

        # Regular idle trigger: needs longer idle + random 1-2h delay
        if idle_secs < idle_min:
            continue
        if gid in _idle_scheduled:
            continue

        delay = random.randint(
            config.INDIO_STORY_IDLE_DELAY_MIN,
            config.INDIO_STORY_IDLE_DELAY_MAX,
        )
        _idle_scheduled.add(gid)
        _spawn(_delayed_idle_story(bot, gid, delay))


async def _delayed_idle_story(bot, guild_id: int, delay_sec: int) -> None:
    try:
        await asyncio.sleep(delay_sec)
    except asyncio.CancelledError:
        return

    _idle_scheduled.discard(guild_id)

    if not _can_post_story(guild_id):
        return

    await trigger_story(
        bot, guild_id, config.INDIO_STORY_CHANNEL_ID, trigger_type="idle"
    )


def record_chat_activity(guild_id: int) -> None:
    now = time.time()
    _last_chat_activity[guild_id] = now
    _idle_scheduled.discard(guild_id)
    if _last_story_at.get(guild_id, 0) > 0:
        _messages_since_story[guild_id] = _messages_since_story.get(guild_id, 0) + 1


def check_voice_trigger(guild_id: int, channel) -> bool:
    now = time.time()
    if now - _last_voice_trigger.get(guild_id, 0.0) < 1800:
        return False
    if channel is None:
        return False
    humans = sum(1 for m in channel.members if not m.bot)
    if humans >= config.INDIO_STORY_VOICE_MIN_MEMBERS:
        _last_voice_trigger[guild_id] = now
        return True
    return False
