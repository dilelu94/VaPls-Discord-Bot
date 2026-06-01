"""Human-in-the-loop curation of the decifrar cache.

Each call to ``geminiCommand.decifrarTranscripcion`` appends a (raw, decifrado)
entry to ``data/decifrar_log.jsonl``. With probability ``1/SAMPLE_RATE`` the
bot posts that entry to ``DECIFRAR_VOTE_CHANNEL_ID`` with 👍/👎 buttons.

Vote resolution:
  - Net 👍 ≥ THRESHOLD  → entry marked ``status="approved"`` in the JSONL,
    promoted to the in-memory cache immediately, and seeded into the cache
    again at every future startup. The Discord message gets edited to "✅".
  - Net 👎 ≥ THRESHOLD  → entry deleted from JSONL; the Discord message
    deleted. The raw will be decifrado from scratch next time it appears.
  - No resolution within TIMEOUT_HOURS → entry deleted (best-effort message
    cleanup too). Treated like a 👎 — the curator simply didn't bother.

State that lives in memory only (lost on restart, by design):
  - The set of voters per message (for one-vote-per-user dedup).

The whole feature is opt-in via ``DECIFRAR_VOTE_ENABLED``. When disabled,
every entry point is a no-op so the wrapping ``decifrarTranscripcion`` path
pays nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from typing import Optional

import discord

import config

logger = logging.getLogger("decifrarVoting")


# ---- State ---------------------------------------------------------------

_lock = asyncio.Lock()
_entries: list[dict] = []                            # mirror of the JSONL
_voters: dict[int, dict[str, set[int]]] = {}         # msg_id → {"up"/"down" → user_ids}
_bot: Optional["discord.Bot"] = None
_started = False


# ---- Disk I/O ------------------------------------------------------------

def _normalize_key(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").lower()).strip()


def _load_from_disk() -> list[dict]:
    path = config.DECIFRAR_LOG_PATH
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("decifrar_log: skipping malformed line")
    except OSError:
        logger.exception("decifrar_log: failed to read %s", path)
    return out


def _save_unlocked() -> None:
    path = config.DECIFRAR_LOG_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for e in _entries:
            fh.write(json.dumps(e, ensure_ascii=False))
            fh.write("\n")
    os.replace(tmp, path)


def _trim_unlocked() -> None:
    cap = config.DECIFRAR_LOG_MAX_LINES
    if cap <= 0 or len(_entries) <= cap:
        return
    pending = [(i, e) for i, e in enumerate(_entries) if e.get("status") == "pending"]
    excess = len(_entries) - cap
    pending.sort(key=lambda pair: pair[1].get("ts", 0))
    drop_idx = {i for i, _ in pending[:excess]}
    if not drop_idx:
        return
    _entries[:] = [e for i, e in enumerate(_entries) if i not in drop_idx]


# ---- Public entry point --------------------------------------------------

async def record(raw: str, decifrado: str, msg_id: Optional[int] = None, channel_id: Optional[int] = None) -> None:
    """Log a (raw, decifrado) pair and maybe post it to the vote channel or add reactions.

    Safe to call from any decifrado callsite — fully fire-and-forget.

    **Inline voting** (msg_id + channel_id present): always records the entry
    and adds 👍/👎 reactions to the transcript message so users can curate the
    ASR cache directly in the transcript channel.  This path does NOT require
    ``DECIFRAR_VOTE_ENABLED`` — it's a lightweight overlay on the existing
    transcript flow.

    **Legacy channel voting** (no msg_id): gated by ``DECIFRAR_VOTE_ENABLED``
    and ``DECIFRAR_VOTE_SAMPLE_RATE``.  Posts a standalone message to the
    dedicated vote channel.
    """
    if not raw or not decifrado:
        return
    norm = _normalize_key(raw)

    inline = msg_id is not None and channel_id is not None

    # Legacy path requires the feature flag; inline path always runs.
    if not inline and not getattr(config, "DECIFRAR_VOTE_ENABLED", False):
        return

    async with _lock:
        for e in _entries:
            if e.get("raw_key") == norm and e.get("decifrado") == decifrado:
                return
        entry = {
            "id": str(uuid.uuid4()),
            "ts": time.time(),
            "raw": raw,
            "raw_key": norm,
            "decifrado": decifrado,
            "status": "pending",
            "msg_id": msg_id if inline else None,
            "channel_id": channel_id,
        }
        _entries.append(entry)
        _trim_unlocked()
        try:
            _save_unlocked()
        except OSError:
            logger.exception("decifrar_log: save failed")
            return
        entry_id = entry["id"]

    if inline:
        # Always add reactions for inline transcript messages.
        asyncio.create_task(_add_inline_reactions(channel_id, msg_id))
    else:
        # Legacy: probabilistic posting to the dedicated vote channel.
        rate = max(1, int(config.DECIFRAR_VOTE_SAMPLE_RATE))
        if random.randint(1, rate) == 1:
            asyncio.create_task(_maybe_post_sample(entry_id))


async def _add_inline_reactions(channel_id: int, message_id: int) -> None:
    if _bot is None:
        return
    try:
        channel = _bot.get_channel(channel_id) or await _bot.fetch_channel(channel_id)
        if channel is not None:
            msg = await channel.fetch_message(message_id)
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")
            await msg.add_reaction("❌")
    except Exception:
        logger.exception("decifrar_vote: failed to add inline reactions to %s", message_id)



async def _maybe_post_sample(entry_id: str) -> None:
    rate = max(1, int(config.DECIFRAR_VOTE_SAMPLE_RATE))
    if random.randint(1, rate) != 1:
        return
    if _bot is None:
        return
    channel_id = config.DECIFRAR_VOTE_CHANNEL_ID
    if not channel_id:
        return
    async with _lock:
        entry = next((e for e in _entries if e.get("id") == entry_id), None)
        if entry is None or entry.get("status") != "pending" or entry.get("msg_id"):
            return
        snapshot = dict(entry)
    channel = _bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await _bot.fetch_channel(channel_id)
        except Exception:
            logger.warning("decifrar_vote: channel %s not accessible", channel_id)
            return
    content = _format_message(snapshot)
    try:
        msg = await channel.send(content=content, view=VoteView())
    except Exception:
        logger.exception("decifrar_vote: failed to send")
        return
    async with _lock:
        for e in _entries:
            if e.get("id") == entry_id:
                e["msg_id"] = msg.id
                try:
                    _save_unlocked()
                except OSError:
                    logger.exception("decifrar_log: save failed after post")
                break


def _format_message(entry: dict) -> str:
    raw = _clip(entry.get("raw", ""), 600)
    dec = _clip(entry.get("decifrado", ""), 600)
    return (
        f"🎤 **Raw:** `{raw}`\n"
        f"🧠 **Decifrado:** `{dec}`\n"
        f"_¿el decifrado captó bien lo que se dijo?_"
    )


def _clip(s: str, n: int) -> str:
    # Backticks would break the inline code block, so swap them for a
    # visually similar character before we ever ship the string to Discord.
    s = (s or "").replace("`", "ʼ")
    return s if len(s) <= n else s[: n - 1] + "…"


# ---- Voting --------------------------------------------------------------

class VoteView(discord.ui.View):
    """Persistent view for vote buttons. Registered once at startup via
    ``bot.add_view(VoteView())`` so the buttons survive bot restarts.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="👍 OK", style=discord.ButtonStyle.success,
        custom_id="decifrar_vote_up",
    )
    async def vote_up(self, button: discord.ui.Button,
                      interaction: discord.Interaction) -> None:
        await _handle_vote(interaction, "up")

    @discord.ui.button(
        label="👎 Mal", style=discord.ButtonStyle.danger,
        custom_id="decifrar_vote_down",
    )
    async def vote_down(self, button: discord.ui.Button,
                        interaction: discord.Interaction) -> None:
        await _handle_vote(interaction, "down")

    @discord.ui.button(
        label="❌ Falso Positivo", style=discord.ButtonStyle.secondary,
        custom_id="decifrar_vote_fp",
    )
    async def vote_fp(self, button: discord.ui.Button,
                      interaction: discord.Interaction) -> None:
        await _handle_vote(interaction, "fp")


async def _handle_vote(interaction: discord.Interaction, vote_type: str | int) -> None:
    if vote_type == 1:
        vote_type = "up"
    elif vote_type == -1:
        vote_type = "down"
    user_id = getattr(getattr(interaction, "user", None), "id", 0)
    msg = getattr(interaction, "message", None)
    msg_id = getattr(msg, "id", 0)
    if not msg_id:
        try:
            await interaction.response.send_message("error: sin message id", ephemeral=True)
        except Exception:
            pass
        return
    voters = _voters.setdefault(msg_id, {"up": set(), "down": set(), "fp": set()})
    if "fp" not in voters:
        voters["fp"] = set()

    if user_id in voters[vote_type]:
        emoji_map = {"up": "👍", "down": "👎", "fp": "❌"}
        try:
            await interaction.response.send_message(
                f"ya votaste {emoji_map.get(vote_type)}", ephemeral=True,
            )
        except Exception:
            pass
        return
    voters[vote_type].add(user_id)
    for other in {"up", "down", "fp"}:
        if other != vote_type:
            voters[other].discard(user_id)
    up = len(voters["up"])
    down = len(voters["down"])
    fp = len(voters["fp"])
    threshold = max(1, int(config.DECIFRAR_VOTE_THRESHOLD))
    try:
        await interaction.response.defer()
    except Exception:
        pass
    if up - down >= threshold:
        await _resolve_approved(msg_id, msg)
    elif down - up >= threshold:
        await _resolve_rejected(msg_id, msg)
    elif fp >= threshold:
        await _resolve_rejected(msg_id, msg)


async def _resolve_approved(msg_id: int, msg) -> None:
    async with _lock:
        entry = next((e for e in _entries if e.get("msg_id") == msg_id), None)
        if entry is None or entry.get("status") != "pending":
            return
        entry["status"] = "approved"
        try:
            _save_unlocked()
        except OSError:
            logger.exception("decifrar_log: save failed on approve")
    _promote_to_cache(entry["raw"], entry["decifrado"])
    _voters.pop(msg_id, None)
    if msg is not None:
        try:
            if _bot is None or msg.author.id == _bot.user.id:
                await msg.edit(
                    content=(msg.content or "") + "\n\n✅ aprobado — cacheado",
                    view=None,
                )
            else:
                import geminiCommand
                new_content = (msg.content or "") + "\n\n✅ aprobado — cacheado"
                await geminiCommand.relay_transcript_decifrado_raw(
                    channel_id=msg.channel.id,
                    message_id=msg.id,
                    content=new_content,
                )
                try:
                    await msg.clear_reactions()
                except Exception:
                    pass
        except Exception:
            pass


async def _resolve_rejected(msg_id: int, msg) -> None:
    async with _lock:
        before = len(_entries)
        _entries[:] = [e for e in _entries if e.get("msg_id") != msg_id]
        if len(_entries) == before:
            return
        try:
            _save_unlocked()
        except OSError:
            logger.exception("decifrar_log: save failed on reject")
    _voters.pop(msg_id, None)
    if msg is not None:
        try:
            await msg.delete()
        except Exception:
            pass


async def handle_reaction_vote(
    bot: discord.Bot,
    *,
    channel_id: int,
    message_id: int,
    emoji: str,
    user_id: int,
    added: bool,
) -> None:
    """Handle raw reaction add/remove on transcript messages for decifrar voting."""
    if emoji not in ("👍", "👎", "❌"):
        return

    async with _lock:
        entry = next((e for e in _entries if e.get("msg_id") == message_id), None)
        if entry is None or entry.get("status") != "pending":
            return

    voters = _voters.setdefault(message_id, {"up": set(), "down": set(), "fp": set()})
    if "fp" not in voters:
        voters["fp"] = set()

    if added:
        if emoji == "👍":
            voters["up"].add(user_id)
            voters["down"].discard(user_id)
            voters["fp"].discard(user_id)
        elif emoji == "👎":
            voters["down"].add(user_id)
            voters["up"].discard(user_id)
            voters["fp"].discard(user_id)
        elif emoji == "❌":
            voters["fp"].add(user_id)
            voters["up"].discard(user_id)
            voters["down"].discard(user_id)
    else:
        if emoji == "👍":
            voters["up"].discard(user_id)
        elif emoji == "👎":
            voters["down"].discard(user_id)
        elif emoji == "❌":
            voters["fp"].discard(user_id)

    up = len(voters["up"])
    down = len(voters["down"])
    fp = len(voters["fp"])
    threshold = max(1, int(config.DECIFRAR_VOTE_THRESHOLD))

    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await channel.fetch_message(message_id)
    except Exception:
        msg = None

    if up - down >= threshold:
        await _resolve_approved(message_id, msg)
    elif down - up >= threshold:
        await _resolve_rejected(message_id, msg)
    elif fp >= threshold:
        await _resolve_rejected(message_id, msg)



def _promote_to_cache(raw: str, decifrado: str) -> None:
    """Push the approved entry into the live in-memory decifrar cache so the
    next request with the same raw text skips Gemini. Late-imports
    ``geminiCommand`` to avoid a circular dependency at module load time."""
    try:
        import geminiCommand
        geminiCommand.seed_decifrar_cache([(_normalize_key(raw), decifrado)])
    except Exception:
        logger.exception("decifrar_vote: promote-to-cache failed")


# ---- Startup -------------------------------------------------------------

def approved_seed_pairs() -> list[tuple[str, str]]:
    """Read the JSONL straight off disk and return the approved (raw_key,
    decifrado) pairs, sorted oldest → newest, capped at
    ``DECIFRAR_CACHE_SEED_MAX``. Safe to call before ``start()``.
    """
    entries = _load_from_disk()
    approved = [e for e in entries if e.get("status") == "approved"]
    approved.sort(key=lambda e: e.get("ts", 0))
    cap = max(0, int(getattr(config, "DECIFRAR_CACHE_SEED_MAX", 128)))
    if cap and len(approved) > cap:
        approved = approved[-cap:]
    return [(_normalize_key(e.get("raw", "")), e.get("decifrado", ""))
            for e in approved
            if e.get("raw") and e.get("decifrado")]


async def start(bot: "discord.Bot") -> None:
    """Idempotent startup hook: register the persistent vote view, hydrate
    the in-memory mirror from disk, seed the decifrar cache with approved
    entries, and launch the background expiry sweeper. Calling this when
    ``DECIFRAR_VOTE_ENABLED`` is false binds the bot reference but does
    nothing else, so toggling the env var requires a restart.
    """
    global _bot, _started, _entries
    if _started:
        return
    _bot = bot
    if not getattr(config, "DECIFRAR_VOTE_ENABLED", False):
        _started = True
        return
    async with _lock:
        _entries = _load_from_disk()
    pairs = approved_seed_pairs()
    if pairs:
        try:
            import geminiCommand
            geminiCommand.seed_decifrar_cache(pairs)
        except Exception:
            logger.exception("decifrar_vote: initial seed failed")
    try:
        bot.add_view(VoteView())
    except Exception:
        logger.exception("decifrar_vote: failed to register VoteView")
    asyncio.create_task(_expiry_loop())
    _started = True
    logger.info("decifrar_voting started; %d entries loaded, %d approved seeded",
                len(_entries), len(pairs))


async def _expiry_loop() -> None:
    """Hourly sweep — drop pending entries older than the timeout. Best-effort
    cleanup of the corresponding Discord messages."""
    while True:
        try:
            await asyncio.sleep(3600)
            ttl = config.DECIFRAR_VOTE_TIMEOUT_HOURS * 3600
            if ttl <= 0:
                continue
            now = time.time()
            async with _lock:
                expired = [e for e in _entries
                           if e.get("status") == "pending"
                           and (now - e.get("ts", now)) > ttl]
                if not expired:
                    continue
                expired_ids = {e["id"] for e in expired}
                _entries[:] = [e for e in _entries if e.get("id") not in expired_ids]
                try:
                    _save_unlocked()
                except OSError:
                    logger.exception("decifrar_log: save failed on expiry")
            for e in expired:
                msg_id = e.get("msg_id")
                if not (msg_id and _bot):
                    continue
                try:
                    channel = _bot.get_channel(config.DECIFRAR_VOTE_CHANNEL_ID)
                    if channel is None:
                        continue
                    msg = await channel.fetch_message(msg_id)
                    await msg.delete()
                except Exception:
                    pass
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("decifrar_vote: expiry loop error")


# ---- Test hook -----------------------------------------------------------

def _reset_for_tests() -> None:
    global _entries, _voters, _bot, _started
    _entries = []
    _voters = {}
    _bot = None
    _started = False
