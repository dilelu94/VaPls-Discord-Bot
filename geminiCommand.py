"""Slash command logic for /vapls and /indio.

Both commands ask Google Gemini for a reply. /vapls is stateless (no memory).
/indio keeps a short verbatim conversation history per guild PLUS a compressed
"long-term" memory (rasgos por usuario, anécdotas, chistes internos). When the
history grows past a threshold, the oldest turns are distilled into the
long-term notes via a separate Gemini call (fire-and-forget) before being
discarded — so the indio feels like a friend that remembers the group.

Depends on geminiClient and analytics.
"""
import asyncio
import collections
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from typing import Optional

import aiohttp
import discord

import analytics
import config
import geminiClient
import geminiKeys

try:
    from users import USERS as _USERS
except Exception:
    _USERS: dict[int, dict] = {}

try:
    from users import GROUP_LORE as _GROUP_LORE
except Exception:
    _GROUP_LORE: dict[str, list[str]] = {}

logger = logging.getLogger("bot.gemini")

VAPLS_SYSTEM = """\
Sos el bot del servidor de Discord "VaPls". Tu rol es ayudar a los amigos del \
server con preguntas, traducciones, datos curiosos o lo que necesiten. Sos \
amigable, directo, y respondés en español rioplatense (voseo). Usás emojis con \
moderación: uno o dos por respuesta máximo. Tus respuestas son concisas: por \
defecto no más de 4 párrafos cortos. Si te piden código, lo devolvés bien \
formateado en bloques de Discord (```lang ... ```). No inventás información: \
si no sabés algo, lo decís. No tenés acceso a internet en tiempo real ni al \
estado del servidor. No te hagas pasar por un humano: sos un bot y está bien \
que se note.
"""

INDIO_SYSTEM = """\
Sos "el indio", un personaje del grupo de amigos en este server de Discord. NO \
sos un bot: sos un pibe más del grupo, charlatán, divertido, con buena onda. \
Importante: VaPls SÍ es un bot, el bot oficial del grupo que corre los \
comandos como /play, /vapls, /indio, etc. NO trates a VaPls como persona. \
\
Sos bastante más grande que el grupo: tenés más de 30 años más que cualquiera \
de tus amigos, sos el viejo veterano de la barra. Eso lo podés referenciar \
con onda cuando viene al caso (sin restregarlo en cada mensaje). \
\
Cada usuario del grupo tiene un APODO (lo ves entre corchetes en cada mensaje, \
ej. "[Miles]: ...") y, para varios, un NOMBRE REAL distinto (aparece en sus \
rasgos como "nombre real: X"). Usá el nombre real SOLO para inferir si es \
hombre o mujer y hablarle con la concordancia correcta. Llamalos SIEMPRE por \
el apodo, NUNCA por el nombre real (queda raro y los conocés por el apodo). \
\
Algunos rasgos vienen prefijados con "(privado, no mencionar)": son contexto \
para vos, te ayudan a responder coherente, pero NO los digas explícitamente \
en el chat. \
\
Si el grupo te pide música/un tema/una canción, usás la tool `play_music`. Si \
te piden un audio/sonido/clip del soundpad, usás la tool `play_sound`. Si te \
piden controlar la música que está sonando, usás `skip_music` (saltear/cambiar), \
`pause_music` (pausar/frenar), `resume_music` (despausar/seguir) o `stop_music` \
(parar y limpiar la cola). Una sola tool por mensaje y solo cuando te lo piden \
de verdad. Antes de llamarla, mandá un texto BREVE de confirmación ("dale, va \
Queen", "tomá milapollo", "dale, salteo") — sin chamuyo. Nunca digas "no puedo" \
o "no me anda": las tools andan, las usás y listo. \
\
Hablás español rioplatense bien casual (voseo, modismos argentinos, muletillas \
como "che", "boludo" usado con afecto, "posta", "una banda", "de una"). \
\
Estás en un chat grupal con varios amigos a la vez. Cada mensaje del grupo te \
llega con el formato "[nombre]: contenido" donde "nombre" es quién habla. Te \
acordás de quién dijo qué y podés referirte a alguien por su nombre si hace \
falta. NO empieces tus respuestas con "[indio]:" ni nada parecido: hablás \
directo, como el indio. Si te hablan a vos directamente, respondé a esa \
persona; si te preguntan por otra, contestá lo que sepas de la conversación \
previa. \
\
Sos opinable pero no agresivo. Si la conversación se pone densa o trolleadora, \
seguís el chiste sin pasarte de mambo. Si te preguntan si sos un bot, esquivás \
con humor pero no mentís de manera ofensiva. Mantenés respuestas cortas, como \
en chat real: 1 a 3 oraciones la mayoría de las veces. Solo te extendés si la \
pregunta lo amerita (explicar algo técnico, contar una anécdota). Tirás algún \
emoji cada tanto, como un pibe en un chat real: ni en cada mensaje ni nunca, \
alguno suelto cuando viene al caso (😂, 👀, 🤡, 🙏, 🔥, 💀, etc.). Si el \
server tiene emojis custom, los CONOCÉS — más abajo te paso la lista — y los \
podés usar pegando EXACTAMENTE el código "<:nombre:id>" (o "<a:nombre:id>" si \
es animado) tal cual aparece en esa lista. Discord solo los renderiza si \
copiás el código completo con los "<", ":" e "id" numérico. No inventes ids \
ni uses ":nombre:" pelado, no funciona. Si te preguntan si viste tal o cual \
emoji o "los nuevos emojis del server", mirá la lista de abajo y respondé en \
base a eso — no hagas el bobo si los tenés a mano, tirá uno o dos pegando el \
código y listo. Nunca rompés el personaje para decir "como modelo de \
lenguaje..." ni nada similar.
"""

_INDIO_TOOLS = [
    {
        "name": "play_music",
        "description": (
            "Reproducir una canción/tema en el canal de voz #sick-tunes vía "
            "el comando /play. Usala cuando el grupo te pide música, un "
            "tema, una canción, o que pongas algo (artista, género, mood)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Búsqueda en YouTube o URL. Usá lo que dijeron tal "
                        "cual (ej: 'Dua Lipa', 'jazz tranquilo', "
                        "'Despacito'). Si hay varios resultados, el sistema "
                        "le pregunta al que pidió cuál quiere; si es una URL "
                        "la reproduce directo. No elijas vos el tema."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "play_sound",
        "description": (
            "Reproducir un clip corto del soundpad (audio meme/efecto) en "
            "el canal de voz. Usala cuando te piden un audio, sonido, "
            "clip o meme por nombre."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name": {
                    "type": "STRING",
                    "description": (
                        "Nombre o palabra clave del clip (fuzzy match). "
                        "Ej: 'milapollo', 'risas', 'aplausos'."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "skip_music",
        "description": (
            "Saltear el tema actual y pasar al siguiente de la cola. "
            "Usala cuando piden 'saltea', 'skip', 'pasá al que sigue', "
            "'el siguiente', 'cambiá de tema'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "pause_music",
        "description": (
            "Pausar la música que está sonando ahora. Usala cuando "
            "piden 'pausá', 'frená', 'pará un toque'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "resume_music",
        "description": (
            "Despausar / retomar la música que estaba pausada. Usala "
            "cuando piden 'seguí', 'dale', 'reanudá', 'volvé a poner'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "stop_music",
        "description": (
            "Parar la música y vaciar la cola. Usala cuando piden "
            "'pará la música', 'basta', 'cortala', 'limpiá la cola'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
]


_STORED_MSG_MAX_CHARS = 1500
_HISTORY_TTL_SEC = 6 * 3600
_DISCORD_CHUNK_LIMIT = 1990
_MAX_CHUNKS = 4

# Short-term history bounds (in turns; each /indio call appends 2 turns).
# When history grows past the threshold we kick off a compression task that
# distills the oldest turns into the long-term notes. HARD_CAP is the safety
# slice that bounds RAM if compression keeps failing.
_HISTORY_COMPRESS_THRESHOLD = 30  # ~15 mensajes user + 15 model
_HISTORY_KEEP_AFTER_COMPRESS = 14  # se queda con los ~7 más recientes user+model
_HISTORY_HARD_CAP = 50

# Long-term memory bounds.
_LONG_TERM_MAX_CHARS = 8000        # JSON dumpeado no debe pasar de esto
_LT_TRAITS_PER_USER = 5
_LT_QUESTIONS_PER_USER = 5
_LT_ANECDOTES_PER_USER = 5
_LT_GROUP_EVENTS = 10
_LT_JOKES = 10

_indio_history: dict[str, list[dict]] = {}
_indio_last_seen: dict[str, float] = {}
_indio_long_term: dict[str, dict] = {}
_indio_locks: dict[str, asyncio.Lock] = {}
_persist_lock = asyncio.Lock()
# Per-key flag: a compression task is in-flight, don't spawn another.
_indio_compressing: set[str] = set()
# "Main characters" roster persisted alongside long-term memory. Refreshed
# at most once per ``_ROSTER_REFRESH_INTERVAL_SEC``; see _maybe_refresh_current_members.
_indio_current_members: dict[str, list[str]] = {}
_indio_members_refreshed_at: dict[str, float] = {}

# Pending music disambiguation. When the indio is asked for a song and the
# search returns several candidates, we DON'T play one blindly — we list them
# and remember the options, keyed by (memory bucket, speaker name) so only the
# person who asked resolves their own choice. The next message from that speaker
# is interpreted as the selection. Entries expire after the TTL so a forgotten
# question doesn't hijack a later, unrelated message.
_PENDING_CHOICE_TTL_SEC = 120
# How many candidates to offer. Kept in sync with playCommand's /play picker.
_MUSIC_CHOICE_COUNT = 5
_indio_pending_choice: dict[tuple[str, str], dict] = {}


def _load_indio_state() -> None:
    """Load history+last_seen+long_term from disk on startup. Silently no-ops
    if the file is missing or unreadable — memory just starts empty."""
    path = config.INDIO_MEMORY_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        logger.exception("indio memory load failed at %s", path)
        return
    entries = data.get("entries", {})
    now = time.time()
    loaded = 0
    for key, val in entries.items():
        last_seen = float(val.get("last_seen", 0))
        history = val.get("history", [])
        long_term = val.get("long_term") or {}
        current_members = val.get("current_members") or []
        current_members_at = float(val.get("current_members_refreshed_at", 0) or 0)
        keep_short_term = (now - last_seen <= _HISTORY_TTL_SEC)
        if keep_short_term:
            if isinstance(history, list) and history:
                _indio_history[key] = history
                _indio_last_seen[key] = last_seen
                loaded += 1
        if isinstance(long_term, dict) and long_term:
            _indio_long_term[key] = long_term
        if isinstance(current_members, list) and current_members:
            _indio_current_members[key] = [str(n) for n in current_members if n]
            _indio_members_refreshed_at[key] = current_members_at
    if loaded or _indio_long_term or _indio_current_members:
        logger.info("indio memory: loaded %d entries (long_term=%d, roster=%d) from %s",
                    loaded, len(_indio_long_term), len(_indio_current_members), path)


async def _persist_indio_state() -> None:
    """Atomic write of the full indio state to disk. Held under _persist_lock
    so concurrent turns don't clobber each other's writes."""
    path = config.INDIO_MEMORY_PATH
    async with _persist_lock:
        keys = set(_indio_history) | set(_indio_long_term) | set(_indio_current_members)
        payload = {
            "entries": {
                k: {
                    "history": _indio_history.get(k, []),
                    "last_seen": _indio_last_seen.get(k, 0.0),
                    "long_term": _indio_long_term.get(k, {}),
                    "current_members": _indio_current_members.get(k, []),
                    "current_members_refreshed_at": _indio_members_refreshed_at.get(k, 0.0),
                }
                for k in keys
            }
        }
        try:
            await asyncio.to_thread(_write_json_atomic, path, payload)
        except Exception:
            logger.exception("indio memory persist failed at %s", path)


def _write_json_atomic(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".indio_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_load_indio_state()


def _indio_memory_key(ctx: discord.ApplicationContext) -> str:
    """Build the memory bucket key for the Indio persona.

    Args:
        ctx: Discord application context.

    Returns:
        A string key scoped to the guild (or DM if no guild).
    """
    guild = getattr(ctx, "guild", None)
    if guild is not None and getattr(guild, "id", None) is not None:
        return f"guild-{guild.id}"
    return f"dm-{getattr(ctx.author, 'id', 'unknown')}"


def _split_for_discord(text: str) -> list[str]:
    """Split text into Discord-sized chunks.

    Args:
        text: Full response text.

    Returns:
        List of chunks capped at _MAX_CHUNKS.

    Side Effects:
        None. The last chunk may be truncated with an ellipsis.
    """
    if len(text) <= _DISCORD_CHUNK_LIMIT:
        return [text]

    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > _DISCORD_CHUNK_LIMIT:
            if buf:
                chunks.append(buf)
                buf = ""
            while len(line) > _DISCORD_CHUNK_LIMIT:
                chunks.append(line[:_DISCORD_CHUNK_LIMIT])
                line = line[_DISCORD_CHUNK_LIMIT:]
        buf += line
        if len(chunks) >= _MAX_CHUNKS:
            break
    if buf and len(chunks) < _MAX_CHUNKS:
        chunks.append(buf)

    if len(chunks) >= _MAX_CHUNKS:
        marker = "\n…(truncado)"
        last = chunks[_MAX_CHUNKS - 1]
        if len(last) + len(marker) > _DISCORD_CHUNK_LIMIT:
            last = last[: _DISCORD_CHUNK_LIMIT - len(marker)]
        chunks = chunks[:_MAX_CHUNKS - 1] + [last + marker]

    return chunks


def _evict_stale_indio() -> None:
    """Drop stale short-term Indio history while keeping long-term memory.

    Short-term verbatim history is evicted once it passes the TTL, but the
    per-guild ``long_term`` memory and ``last_seen`` survive so the indio keeps
    remembering the group like a friend. Keys currently being compressed are
    skipped, and a lock is only released when nothing relevant remains.

    Returns:
        None.

    Side Effects:
        Mutates in-memory history/lock dictionaries.
    """
    now = time.time()
    for key in list(_indio_last_seen.keys()):
        if now - _indio_last_seen[key] <= _HISTORY_TTL_SEC:
            continue
        if key in _indio_compressing:
            continue
        _indio_history.pop(key, None)
        # _indio_long_term y _indio_last_seen sobreviven.
        # Lock se libera solo si no quedó nada relevante.
        if key not in _indio_long_term:
            _indio_locks.pop(key, None)
    # Sweep abandoned pending music choices so they don't pile up: an entry
    # only ever gets touched again if that same requester speaks, so without
    # this a forgotten "¿cuál querés?" would live forever despite the TTL.
    for ck in list(_indio_pending_choice.keys()):
        if now - _indio_pending_choice[ck].get("ts", 0.0) > _PENDING_CHOICE_TTL_SEC:
            del _indio_pending_choice[ck]


async def _send_reply(ctx: discord.ApplicationContext, text: str) -> int:
    """Send a possibly multi-part reply to Discord.

    Args:
        ctx: Discord application context.
        text: Full response text.

    Returns:
        Number of chunks sent.

    Side Effects:
        Sends follow-up messages via Discord.

    Async:
        This function is a coroutine and must be awaited.
    """
    chunks = _split_for_discord(text)
    for c in chunks:
        await ctx.followup.send(c)
    return len(chunks)


def _format_user_header(ctx: discord.ApplicationContext, pregunta: str) -> str:
    """Format the user header and quoted question for responses.

    Args:
        ctx: Discord application context.
        pregunta: Original user question.

    Returns:
        A formatted header string for the reply.
    """
    name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", "alguien")
    lines = (pregunta or "").splitlines() or [""]
    quoted = "\n".join(f"> {ln}" for ln in lines)
    return f"**{name}** preguntó:\n{quoted}\n\n"


_GUILD_EMOJI_LIMIT = 40


def _format_guild_emojis(guild) -> str:
    """Render the guild's custom emojis as a prompt block so the indio can
    drop them into replies. Each entry shows the exact "<:name:id>" code that
    Discord needs to render the image — Gemini won't guess IDs correctly, so
    we hand them over verbatim. Returns "" if there are no usable emojis."""
    emojis = getattr(guild, "emojis", None) or []
    lines: list[str] = []
    for e in emojis:
        if not getattr(e, "available", True):
            continue
        if getattr(e, "id", None) is None or not getattr(e, "name", ""):
            continue
        prefix = "a" if getattr(e, "animated", False) else ""
        lines.append(f"- :{e.name}: → <{prefix}:{e.name}:{e.id}>")
        if len(lines) >= _GUILD_EMOJI_LIMIT:
            break
    if not lines:
        return ""
    return "Emojis custom del server (pegá el código completo tal cual):\n" + "\n".join(lines)


_ROSTER_REFRESH_INTERVAL_SEC = 24 * 3600  # refresh from users.py once per day
_roster_lock = asyncio.Lock()


def _names_from_users_py() -> list[str]:
    """Read the friend roster from the static users.py mapping. We use this as
    the source of truth because discord.py-self can't reliably enumerate every
    guild member from a user account (the cache is partial and fetch_members
    only returns members the gateway has surfaced)."""
    return [info["name"] for info in _USERS.values() if isinstance(info, dict) and info.get("name")]


async def _maybe_refresh_current_members(mem_key: str, guild_id: Optional[int]) -> None:
    """Refresh the cached friend roster for a guild at most once per
    ``_ROSTER_REFRESH_INTERVAL_SEC``. The names come from users.py and live
    alongside the indio's long-term memory, persisted to disk so they
    survive restarts. The indio doesn't "read" the list on every call — he
    knows who they are because it's already in his memory."""
    if guild_id is None:
        return
    expected = _names_from_users_py()
    if not expected:
        return
    now = time.time()
    last = _indio_members_refreshed_at.get(mem_key, 0.0)
    current = _indio_current_members.get(mem_key)
    # Refresh if (a) the TTL elapsed, (b) we never refreshed, or
    # (c) users.py was edited and the stored list no longer matches.
    if (now - last < _ROSTER_REFRESH_INTERVAL_SEC
            and current == expected):
        return
    async with _roster_lock:
        last = _indio_members_refreshed_at.get(mem_key, 0.0)
        current = _indio_current_members.get(mem_key)
        if (now - last < _ROSTER_REFRESH_INTERVAL_SEC
                and current == expected):
            return
        previous = current
        _indio_current_members[mem_key] = expected
        _indio_members_refreshed_at[mem_key] = time.time()
    if previous != expected:
        await _persist_indio_state()
        logger.info("indio: refreshed current_members for %s (%d names from users.py)",
                    mem_key, len(expected))


def _static_user_traits() -> dict[str, dict[str, list[str]]]:
    """Pull manual traits/preguntas/anecdotas from users.py. Each entry can
    optionally carry ``traits``, ``preguntas_tipicas`` and ``anecdotas``
    lists; these are merged into the long-term render every time the indio
    answers and are never overwritten by Gemini's compression cycle."""
    out: dict[str, dict[str, list[str]]] = {}
    for info in _USERS.values():
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if not name:
            continue
        out[name] = {
            "traits": [str(t) for t in (info.get("traits") or []) if t],
            "preguntas_tipicas": [str(t) for t in (info.get("preguntas_tipicas") or []) if t],
            "anecdotas": [str(t) for t in (info.get("anecdotas") or []) if t],
        }
    return out


def _block_lists_by_name() -> dict[str, list[str]]:
    """Mapa apodo -> lista de substrings (lowercase) que hay que filtrar de
    la memoria dinámica. Usado para scrubear facts viejos/incorrectos sin
    tener que limpiar a mano el indio_memory.json del server."""
    out: dict[str, list[str]] = {}
    for info in _USERS.values():
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        blocks = info.get("block_dynamic_substrings") or []
        if not name or not blocks:
            continue
        out[str(name)] = [str(b).lower() for b in blocks if b]
    return out


def _merge_user_dossiers(lt_users: dict) -> dict[str, dict[str, list[str]]]:
    """Combine the static per-user traits from users.py with whatever Gemini
    has distilled in long-term memory. Static entries provide a baseline; the
    distilled additions are appended without duplicates. Items in dynamic
    memory matching a user's ``block_dynamic_substrings`` are filtered out."""
    merged = _static_user_traits()
    blocks_by_name = _block_lists_by_name()
    if isinstance(lt_users, dict):
        for name, data in lt_users.items():
            if not isinstance(data, dict):
                continue
            name_str = str(name)
            blocks = blocks_by_name.get(name_str, [])
            bucket = merged.setdefault(name_str, {
                "traits": [], "preguntas_tipicas": [], "anecdotas": [],
            })
            for key in ("traits", "preguntas_tipicas", "anecdotas"):
                existing = bucket.setdefault(key, [])
                for item in (data.get(key) or []):
                    s = str(item)
                    if not s or s in existing:
                        continue
                    if blocks and any(b in s.lower() for b in blocks):
                        continue
                    existing.append(s)
    return merged


def _format_long_term(lt: dict, current_members: Optional[list[str]] = None) -> str:
    """Render long-term memory as a compact Spanish block to inject into the
    indio's system instruction. Natural-language form (no JSON) so the model
    integrates it like context, not data.

    ``current_members`` is the friend roster (from users.py), rendered as a
    short header so the indio always knows who his amigos are from his own
    memory. Per-user dossiers merge static traits (users.py) with Gemini's
    distilled long-term data."""
    sections: list[str] = []
    if current_members:
        sections.append(
            "Mis amigos son: " + ", ".join(current_members) + "."
        )
    lt = lt or {}
    user_dossiers = _merge_user_dossiers(lt.get("users") or {})
    if user_dossiers:
        user_lines = ["Lo que sabés de cada uno:"]
        for name, data in user_dossiers.items():
            traits = data.get("traits") or []
            qs = data.get("preguntas_tipicas") or []
            anec = data.get("anecdotas") or []
            chunk = [f"- {name}:"]
            if traits:
                chunk.append(f"   rasgos: {'; '.join(traits)}")
            if qs:
                chunk.append(f"   suele preguntar sobre: {'; '.join(qs)}")
            if anec:
                chunk.append(f"   anécdotas: {'; '.join(anec)}")
            if len(chunk) > 1:
                user_lines.extend(chunk)
        if len(user_lines) > 1:
            sections.append("\n".join(user_lines))
    # Merge static group lore (users.py:GROUP_LORE) with whatever Gemini has
    # distilled in long_term. Static items go first; dynamic ones are appended
    # without duplicates.
    static_events = [str(x) for x in (_GROUP_LORE.get("eventos_del_grupo") or []) if x]
    lt_events = [str(x) for x in (lt.get("eventos_del_grupo") or []) if x]
    events = list(static_events)
    for e in lt_events:
        if e not in events:
            events.append(e)
    if events:
        sections.append("Cosas que pasaron en el grupo:\n" + "\n".join(f"- {e}" for e in events))

    static_jokes = [str(x) for x in (_GROUP_LORE.get("chistes_internos") or []) if x]
    lt_jokes = [str(x) for x in (lt.get("chistes_internos") or []) if x]
    jokes = list(static_jokes)
    for j in lt_jokes:
        if j not in jokes:
            jokes.append(j)
    if jokes:
        sections.append("Chistes internos del grupo:\n" + "\n".join(f"- {j}" for j in jokes))
    return "\n\n".join(sections)


_COMPRESS_SYSTEM = """\
Sos un asistente que mantiene una memoria a largo plazo sobre un grupo de \
amigos en un server de Discord. Recibís (a) la memoria actual en JSON y (b) \
una conversación nueva del grupo. Tu trabajo es devolver SOLO un JSON \
actualizado, sin texto adicional ni bloques markdown, con esta estructura \
exacta:

{
  "users": {
    "<nombre>": {
      "traits": ["rasgos de personalidad o intereses"],
      "preguntas_tipicas": ["qué tipo de cosas suele preguntar/decir"],
      "anecdotas": ["momentos del grupo que lo involucran"]
    }
  },
  "eventos_del_grupo": ["cosas memorables que pasaron en el chat"],
  "chistes_internos": ["chistes recurrentes o referencias del grupo"]
}

Reglas estrictas:
- NO inventes datos. Solo guardás lo que aparece textualmente o lo que se \
  deduce directamente de la conversación.
- Mantenés los datos previos a menos que la conversación los contradiga.
- Cada string ≤120 caracteres.
- Máx %d rasgos, %d preguntas_tipicas y %d anecdotas por usuario.
- Máx %d eventos_del_grupo y %d chistes_internos en total.
- Conservás los nombres tal cual aparecen entre corchetes ("[nombre]: ...").
- No incluyas al "indio" como usuario (es el bot, no un miembro del grupo).
- Español rioplatense, casual, conciso.
- Devolvé SOLO el JSON. Sin ```json ni explicación.
""" % (_LT_TRAITS_PER_USER, _LT_QUESTIONS_PER_USER, _LT_ANECDOTES_PER_USER,
       _LT_GROUP_EVENTS, _LT_JOKES)


def _extract_json(text: str) -> Optional[dict]:
    """Defensive JSON parser: handles raw JSON, ```json``` fenced blocks, and
    leading/trailing junk. Returns None on any failure."""
    if not text:
        return None
    s = text.strip()
    # Strip markdown fences if present.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # Find the outermost {...}
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _clamp_long_term(lt: dict) -> dict:
    """Enforce structure + per-section caps so a misbehaving Gemini response
    can't blow up the prompt budget."""
    out: dict = {"users": {}, "eventos_del_grupo": [], "chistes_internos": []}
    users = lt.get("users") if isinstance(lt, dict) else None
    if isinstance(users, dict):
        for name, data in list(users.items())[:30]:
            if not isinstance(data, dict):
                continue
            name = str(name)[:60]
            if name.lower() == "indio":
                continue
            traits = [str(t)[:120] for t in (data.get("traits") or []) if t][:_LT_TRAITS_PER_USER]
            qs = [str(t)[:120] for t in (data.get("preguntas_tipicas") or []) if t][:_LT_QUESTIONS_PER_USER]
            anec = [str(t)[:120] for t in (data.get("anecdotas") or []) if t][:_LT_ANECDOTES_PER_USER]
            if traits or qs or anec:
                out["users"][name] = {
                    "traits": traits,
                    "preguntas_tipicas": qs,
                    "anecdotas": anec,
                }
    events = lt.get("eventos_del_grupo") if isinstance(lt, dict) else None
    if isinstance(events, list):
        out["eventos_del_grupo"] = [str(e)[:120] for e in events if e][:_LT_GROUP_EVENTS]
    jokes = lt.get("chistes_internos") if isinstance(lt, dict) else None
    if isinstance(jokes, list):
        out["chistes_internos"] = [str(j)[:120] for j in jokes if j][:_LT_JOKES]
    # Final safety: if still too big after structural clamp, drop oldest events/jokes.
    while len(json.dumps(out, ensure_ascii=False)) > _LONG_TERM_MAX_CHARS:
        if out["eventos_del_grupo"]:
            out["eventos_del_grupo"].pop(0)
        elif out["chistes_internos"]:
            out["chistes_internos"].pop(0)
        elif out["users"]:
            # Drop the oldest-inserted user.
            first = next(iter(out["users"]))
            out["users"].pop(first)
        else:
            break
    return out


def _turns_to_text(turns: list[dict]) -> str:
    """Render a list of {role,parts:[{text}]} turns as plain text for the
    compression prompt."""
    lines: list[str] = []
    for t in turns:
        role = t.get("role", "?")
        parts = t.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text:
            continue
        speaker = "indio" if role == "model" else "grupo"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


async def _compress_long_term(current_lt: dict, old_turns: list[dict]) -> Optional[dict]:
    """Run a Gemini call to fold old verbatim turns into the long-term notes.
    Returns the new long-term dict on success, None on any failure."""
    if not old_turns:
        return None
    convo_text = _turns_to_text(old_turns)
    if not convo_text.strip():
        return None
    user_message = (
        "Memoria actual:\n"
        f"{json.dumps(current_lt or {}, ensure_ascii=False, indent=2)}\n\n"
        "Conversación nueva del grupo:\n"
        f"{convo_text}\n\n"
        "Devolveme SOLO el JSON actualizado."
    )
    try:
        reply = await geminiClient.generate(
            user_message=user_message,
            system_instruction=_COMPRESS_SYSTEM,
            history=None,
            max_output_tokens=2048,
        )
    except geminiClient.GeminiError as e:
        logger.warning("indio compress: gemini failed (%s, status=%s)", e.kind, e.status)
        return None
    except Exception:
        logger.exception("indio compress: unexpected error")
        return None
    parsed = _extract_json(reply.text)
    if parsed is None:
        logger.warning("indio compress: JSON parse failed; raw=%r", reply.text[:200])
        return None
    return _clamp_long_term(parsed)


async def _maybe_compress(mem_key: str) -> None:
    """Fire-and-forget: if the short-term history is over the threshold,
    distill its oldest portion into long-term notes and drop those turns from
    short-term. Safe against concurrent /indio calls because: (a) we hold the
    per-key lock only at read+write points, and (b) we slice from the FRONT by
    count, not by index, so new turns appended during compression aren't lost."""
    if mem_key in _indio_compressing:
        return
    lock = _indio_locks.get(mem_key)
    if lock is None:
        return
    _indio_compressing.add(mem_key)
    try:
        async with lock:
            history = _indio_history.get(mem_key, [])
            if len(history) < _HISTORY_COMPRESS_THRESHOLD:
                return
            # Even count: keep both sides of each user/model pair aligned.
            drop_count = len(history) - _HISTORY_KEEP_AFTER_COMPRESS
            if drop_count % 2 == 1:
                drop_count -= 1
            if drop_count <= 0:
                return
            old_turns = history[:drop_count]
            current_lt = dict(_indio_long_term.get(mem_key, {}))
        new_lt = await _compress_long_term(current_lt, old_turns)
        if new_lt is None:
            logger.info("indio compress: skipped (lt unchanged) for %s", mem_key)
            return
        async with lock:
            history = _indio_history.get(mem_key, [])
            if len(history) >= drop_count:
                _indio_history[mem_key] = history[drop_count:]
            _indio_long_term[mem_key] = new_lt
        await _persist_indio_state()
        logger.info("indio compress: ok for %s (dropped %d turns, users=%d)",
                    mem_key, drop_count, len(new_lt.get("users", {})))
    finally:
        _indio_compressing.discard(mem_key)


# Maps each Gemini tool name to its internal action label and the key under
# ``args`` where the string argument lives (or ``None`` if the tool takes no
# arguments — pure control verbs like skip/pause/resume/stop).
_FUNCTION_CALL_TO_ACTION: dict[str, tuple[str, Optional[str]]] = {
    "play_music": ("PLAY_MUSIC", "query"),
    "play_sound": ("PLAY_SOUND", "name"),
    "skip_music": ("SKIP_MUSIC", None),
    "pause_music": ("PAUSE_MUSIC", None),
    "resume_music": ("RESUME_MUSIC", None),
    "stop_music": ("STOP_MUSIC", None),
}
_ACTION_FALLBACK_TEXT = {
    "PLAY_MUSIC": "🎵 Ahí va",
    "PLAY_SOUND": "🔊 Tomá",
    "SKIP_MUSIC": "⏭️ Siguiente",
    "PAUSE_MUSIC": "⏸️ Pausando",
    "RESUME_MUSIC": "▶️ Dale, va",
    "STOP_MUSIC": "⏹️ Listo",
}
_ACTION_ARG_MAX_CHARS = 200


def _actions_from_function_calls(function_calls: list[dict]) -> list[tuple[str, str]]:
    """Translate Gemini function calls into the (action, arg) tuples that
    ``_dispatch_indio_actions`` understands. For tools without arguments
    the tuple's second element is the empty string. Unknown tool names and
    malformed args are logged and skipped — we don't want a bad call to
    fall through and dispatch with garbage."""
    actions: list[tuple[str, str]] = []
    for call in function_calls or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        mapping = _FUNCTION_CALL_TO_ACTION.get(name.lower())
        if mapping is None:
            logger.warning("indio: unknown tool call '%s' (args=%r)", name, call.get("args"))
            continue
        action, arg_key = mapping
        if arg_key is None:
            # Argument-less control verb (skip/pause/resume/stop).
            actions.append((action, ""))
            continue
        args = call.get("args") or {}
        raw = args.get(arg_key) if isinstance(args, dict) else None
        if not isinstance(raw, str):
            logger.warning("indio: tool %s missing string arg '%s' (got %r)",
                           name, arg_key, raw)
            continue
        arg = raw.strip()[:_ACTION_ARG_MAX_CHARS]
        if not arg:
            logger.warning("indio: tool %s called with empty '%s'", name, arg_key)
            continue
        actions.append((action, arg))
    return actions


def _ensure_reply_text(text: str, actions: list[tuple[str, str]]) -> str:
    """The relay flow and Discord both require non-empty content. When the
    model emits only a function call (no accompanying text), substitute a
    short stock confirmation so the chat shows something."""
    if text:
        return text
    if not actions:
        return text
    fallback = _ACTION_FALLBACK_TEXT.get(actions[0][0], "👍")
    return fallback


async def _invoke_slash_via_userbot(endpoint: str, channel_id: int,
                                    query: str) -> tuple[bool, str]:
    """Ask the userbot to invoke a VaPls slash command (`/play` or
    `/soundpad`) from the real user account, so Discord shows the full
    "Indio used /play" interaction. Returns (ok, message)."""
    if not (config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
        return False, "relay not configured"
    invoke_url = config.INDIO_RELAY_URL.rsplit("/", 1)[0] + "/" + endpoint
    headers = {"X-API-Secret": config.INDIO_RELAY_SECRET}
    payload = {"channel_id": int(channel_id), "query": query}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(invoke_url, json=payload, headers=headers) as resp:
                if resp.status < 400:
                    return True, query
                body = await resp.text()
                return False, f"relay HTTP {resp.status}: {body[:100]}"
    except Exception as exc:
        logger.warning("indio %s relay failed: %s", endpoint, exc)
        return False, f"relay error: {exc}"


async def _dispatch_indio_actions(bot: "discord.Bot",
                                   guild_id: Optional[int],
                                   actions: list[tuple[str, str]],
                                   ) -> list[str]:
    """Run any PLAY_* actions the indio emitted. Both PLAY_MUSIC and
    PLAY_SOUND are invoked through the userbot relay so they show up as
    real "/play" / "/soundpad" slash commands in the chat. Both land in
    ``config.INDIO_PLAY_CHANNEL_ID`` — that's the dedicated room for
    playback regardless of where the conversation is happening. Falls back
    to in-process playback if the relay is unavailable. Returns short
    status strings for logging; the indio's main reply is sent separately."""
    if not actions or guild_id is None or bot is None:
        return []
    statuses: list[str] = []
    try:
        import playCommand
    except Exception:
        logger.exception("indio actions: playCommand import failed")
        return []
    for action, arg in actions:
        try:
            if action == "PLAY_MUSIC":
                ok, msg = await _invoke_slash_via_userbot(
                    "invoke_play",
                    channel_id=config.INDIO_PLAY_CHANNEL_ID,
                    query=arg,
                )
                if not ok:
                    logger.warning(
                        "indio PLAY_MUSIC relay failed (%s); falling back to playFromIndio",
                        msg,
                    )
                    ok, msg = await playCommand.playFromIndio(bot, int(guild_id), arg)
                statuses.append(f"music: {'ok' if ok else 'fail'} — {msg}")
                logger.info("indio PLAY_MUSIC '%s' → ok=%s msg=%s", arg, ok, msg)
            elif action == "PLAY_SOUND":
                ok, msg = await _invoke_slash_via_userbot(
                    "invoke_soundpad",
                    channel_id=config.INDIO_PLAY_CHANNEL_ID,
                    query=arg,
                )
                if not ok:
                    logger.warning(
                        "indio PLAY_SOUND relay failed (%s); falling back to play_clip_by_query",
                        msg,
                    )
                    try:
                        from soundpadCommand import play_clip_by_query
                    except Exception:
                        logger.exception("indio PLAY_SOUND: soundpadCommand import failed")
                        statuses.append("sound: fail — import error")
                        continue
                    guild = bot.get_guild(int(guild_id))
                    if guild is None:
                        statuses.append(f"sound: fail — guild {guild_id} not found")
                        logger.warning("indio PLAY_SOUND: guild %s not found", guild_id)
                        continue
                    played_path = await play_clip_by_query(bot, guild, query=arg)
                    ok = played_path is not None
                    msg = played_path or "no match"
                statuses.append(f"sound: {'ok' if ok else 'fail'} — {msg}")
                logger.info("indio PLAY_SOUND '%s' → ok=%s msg=%s", arg, ok, msg)
            elif action in ("SKIP_MUSIC", "PAUSE_MUSIC", "RESUME_MUSIC", "STOP_MUSIC"):
                # Pure playback controls don't have a slash command equivalent —
                # they only exist as UI buttons on the player. We talk to the
                # GuildPlayer directly. If no player exists for this guild it
                # means nothing was ever queued, so we no-op instead of
                # implicitly creating one.
                player = playCommand.guildPlayers.get(int(guild_id))
                if player is None:
                    statuses.append(f"{action.lower()}: no active player")
                    logger.info("indio %s: no active player for guild %s", action, guild_id)
                    continue
                vc = getattr(player, "vc", None)
                control_ok = False
                if action == "SKIP_MUSIC":
                    await player.skipSong()
                    statuses.append("skip: ok")
                    control_ok = True
                elif action == "STOP_MUSIC":
                    await player.stopPlayback()
                    statuses.append("stop: ok")
                    control_ok = True
                elif action == "PAUSE_MUSIC":
                    if vc and vc.is_playing():
                        await player.togglePausePlay()
                        statuses.append("pause: ok")
                        control_ok = True
                    else:
                        statuses.append("pause: not playing")
                elif action == "RESUME_MUSIC":
                    if vc and vc.is_paused():
                        await player.togglePausePlay()
                        statuses.append("resume: ok")
                        control_ok = True
                    else:
                        statuses.append("resume: not paused")
                logger.info("indio %s → %s", action, statuses[-1])
                # Mirror the control in the playback channel via the userbot
                # so the action is visible in #sick-tunes (these tools don't
                # have slash commands of their own to land there).
                if control_ok:
                    await _relay_to_userbot(
                        config.INDIO_PLAY_CHANNEL_ID,
                        _ACTION_FALLBACK_TEXT.get(action, "👍"),
                        reply_to_id=None,
                    )
        except Exception:
            logger.exception("indio action %s failed", action)
    return statuses


# ---------------------------------------------------------------------------
# Music disambiguation: "che, ¿cuál de estas querés?"
# ---------------------------------------------------------------------------

_CHOICE_CANCEL_WORDS = (
    "ninguna", "ninguno", "ningun", "nada", "deja", "dejalo", "dejala",
    "cancela", "cancelar", "olvidate", "olvidalo", "no quiero", "ni una",
)
# Ordinal/number words → 0-based index. Matched by prefix against each token so
# "primera"/"primero"/"primer" all resolve, etc.
_ORDINAL_STEMS = {
    "primer": 0, "uno": 0,
    "segund": 1, "dos": 1,
    "tercer": 2, "tres": 2,
    "cuart": 3, "cuatro": 3,
    "quint": 4, "cinco": 4,
}
_CHOICE_STOPWORDS = {
    "la", "el", "los", "las", "un", "una", "de", "del", "version", "tema",
    "cancion", "quiero", "poneme", "pone", "poné", "dale", "esa", "ese",
    "esta", "este", "che", "indio", "opcion", "numero", "que", "me", "y", "o",
    "a", "porfa", "porfavor", "mejor",
}


def _normalize_choice(s: str) -> str:
    """Lowercase + strip accents for matching selection utterances."""
    n = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def _looks_like_url(query: str) -> bool:
    q = (query or "").strip()
    return (q.startswith("http://") or q.startswith("https://")
            or q.startswith("ytsearch:"))


def _parse_choice(text: str, candidates: list[dict]):
    """Interpret a selection utterance against the offered candidates.

    Returns the 0-based index of the chosen candidate, the string ``"cancel"``
    when the speaker declined, or ``None`` when the message doesn't look like a
    selection at all (caller should treat it as a normal new message).

    Resolution order: explicit cancel > digit (1..N) > ordinal word
    (primera/segunda/…) > a distinctive word that matches exactly one title.
    """
    if not text or not candidates:
        return None
    norm = _normalize_choice(text)
    for w in _CHOICE_CANCEL_WORDS:
        if w in norm:
            return "cancel"
    n = len(candidates)
    for m in re.finditer(r"\d+", norm):
        v = int(m.group())
        if 1 <= v <= n:
            return v - 1
    tokens = re.findall(r"[a-z]+", norm)
    for tok in tokens:
        for stem, idx in _ORDINAL_STEMS.items():
            if tok.startswith(stem) and idx < n:
                return idx
    # Distinctive-word match against the titles (e.g. "la del vivo",
    # "la de Calamaro"). Only commit when exactly one title wins.
    sig = [t for t in tokens if len(t) >= 3 and t not in _CHOICE_STOPWORDS]
    if sig:
        scores = []
        for c in candidates:
            title = _normalize_choice(c.get("title", ""))
            scores.append(sum(1 for w in sig if w in title))
        best = max(scores)
        if best > 0 and scores.count(best) == 1:
            return scores.index(best)
    return None


def _format_choices(candidates: list[dict]) -> str:
    """Render the "¿cuál querés?" list the indio posts in chat."""
    lines = ["che, ¿cuál de estas querés?"]
    for i, c in enumerate(candidates, 1):
        dur = c.get("duration_string") or ""
        durs = f" [{dur}]" if dur else ""
        lines.append(f"{i}) {c['title']}{durs}")
    lines.append('(decime el número, o "ninguna")')
    return "\n".join(lines)


def _choice_identity(user_id, speaker: str) -> str:
    """Stable identity for a requester's pending choice.

    Prefers the Discord user id (numeric, globally unique) so two members
    sharing a display name can't resolve each other's choice. Falls back to the
    display name only when no id is available. The ``uid:``/``nm:`` prefixes
    keep a numeric display name from ever colliding with a real id.
    """
    if user_id:
        return f"uid:{user_id}"
    return f"nm:{speaker or 'alguien'}"


def _store_pending_choice(mem_key: str, identity: str,
                          candidates: list[dict], guild_id: int) -> None:
    _indio_pending_choice[(mem_key, identity)] = {
        "candidates": candidates,
        "guild_id": guild_id,
        "ts": time.time(),
    }


def _peek_pending_choice(mem_key: str, identity: str) -> Optional[dict]:
    """Return this requester's pending choice without removing it, honouring
    the TTL (an expired entry is dropped and treated as absent). We peek rather
    than pop so an unrecognised follow-up doesn't throw away a still-valid
    choice — only an actual selection or cancel clears it."""
    key = (mem_key, identity)
    entry = _indio_pending_choice.get(key)
    if entry is None:
        return None
    if time.time() - entry.get("ts", 0.0) > _PENDING_CHOICE_TTL_SEC:
        del _indio_pending_choice[key]
        return None
    return entry


def _clear_pending_choice(mem_key: str, identity: str) -> None:
    _indio_pending_choice.pop((mem_key, identity), None)


async def _play_chosen_song(bot, guild_id: int, song: dict) -> None:
    """Play an already-resolved candidate (id + title in hand). We reuse the
    yt-dlp result we got when building the options list, so there is no second
    search and no Gemini call — we just hand the song to the player."""
    import playCommand
    try:
        await playCommand.playFromIndio(
            bot, guild_id, song.get("title") or "tema", songs=[song],
        )
    except Exception:
        logger.exception("indio: play chosen song failed")


async def _maybe_disambiguate_music(bot, guild_id, mem_key, identity,
                                    pending_actions, reply):
    """Intercept a single free-text ``play_music`` so the indio lists the
    matches and lets the requester pick, instead of playing the first hit.

    The search reuses yt-dlp exactly like before (no extra Gemini). With a
    single clear hit we play it directly; with several we ask and remember the
    options under the requester's stable ``identity``. A direct URL, several
    actions at once, or a non-music turn pass through untouched.

    Returns ``(actions_to_dispatch, reply_text)``.
    """
    clean = _strip_indio_prefix(reply.text)
    clean = _ensure_reply_text(clean, pending_actions)
    if guild_id is None:
        return pending_actions, clean
    music = [a for a in pending_actions if a[0] == "PLAY_MUSIC"]
    others = [a for a in pending_actions if a[0] != "PLAY_MUSIC"]
    if len(music) != 1 or others:
        return pending_actions, clean
    query = music[0][1]
    if _looks_like_url(query):
        # An explicit URL has nothing to disambiguate — let it play directly.
        return pending_actions, clean

    import playCommand
    candidates = await playCommand._yt_dlp_search(query, max_results=_MUSIC_CHOICE_COUNT)
    if not candidates:
        return [], "no encontré nada en YouTube con eso, decímelo de otra forma"
    if len(candidates) == 1:
        # One clear match: play it directly with the metadata we already have.
        asyncio.create_task(_play_chosen_song(bot, guild_id, candidates[0]))
        return [], clean
    # Several matches: list them and remember this requester's pending choice.
    _store_pending_choice(mem_key, identity, candidates, guild_id)
    return [], _format_choices(candidates)


async def _resolve_pending_music(bot, mem_key, identity, guild_id, pregunta,
                                 post) -> bool:
    """If this requester has a pending "¿cuál querés?", treat ``pregunta`` as
    the answer. Resolution is pure code (no Gemini); the chosen candidate plays
    directly from the metadata we already have. ``post`` is an async callable
    that delivers a line of text the way the caller normally would.

    We only clear the pending choice on an actual selection or cancel — an
    unrecognised message leaves it in place so a later valid answer still
    works (the TTL/sweep handle abandonment).

    Returns True when the message was consumed as a selection (caller should
    stop and not run a normal Gemini turn), False otherwise.
    """
    entry = _peek_pending_choice(mem_key, identity)
    if entry is None:
        return False
    candidates = entry["candidates"]
    decision = _parse_choice(pregunta, candidates)
    if decision == "cancel":
        _clear_pending_choice(mem_key, identity)
        await post("dale, lo dejo 👍")
        return True
    if isinstance(decision, int) and 0 <= decision < len(candidates):
        _clear_pending_choice(mem_key, identity)
        chosen = candidates[decision]
        await post(f"dale, va: {chosen['title']} 🎵")
        asyncio.create_task(_play_chosen_song(bot, guild_id, chosen))
        return True
    # Not a recognizable selection: leave the choice in place and let the caller
    # process this as a fresh message.
    return False


_INDIO_PREFIX_RE = re.compile(
    r"^\s*[\[\(]?\s*(el\s+)?indio\s*[\]\)]?\s*[:\-—]\s*",
    re.IGNORECASE,
)


def _strip_indio_prefix(text: str) -> str:
    """Drop any "[indio]:" / "Indio:" / "(el indio) -" style prefix the model
    sometimes hallucinates, even though INDIO_SYSTEM tells it not to."""
    if not text:
        return text
    out = _INDIO_PREFIX_RE.sub("", text, count=1)
    return out.lstrip()


async def _relay_to_userbot(channel_id: int, content: str,
                            reply_to_id: Optional[int]) -> bool:
    """POST the indio reply to the userbot's local /say endpoint so it gets
    posted by the real user account. Returns True on success, False on any
    failure (caller should fall back to posting via vapls)."""
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    if not url or not secret:
        return False
    payload = {"channel_id": int(channel_id), "content": content}
    if reply_to_id is not None:
        payload["reply_to_message_id"] = int(reply_to_id)
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("indio relay HTTP %d: %s", resp.status, body[:200])
                    return False
                return True
    except asyncio.TimeoutError:
        logger.warning("indio relay timeout after %.1fs", config.INDIO_RELAY_TIMEOUT)
        return False
    except Exception:
        logger.exception("indio relay failed")
        return False


def _format_contributors_line() -> str:
    """Thin wrapper kept for module-internal callers; logic now lives in
    ``geminiKeys`` so other commands (e.g. /soundpad) can reuse it."""
    return geminiKeys.format_contributors_line()


def _error_message(kind: str, status: Optional[int], persona: str) -> str:
    """Return a user-facing error message for Gemini failures.

    Args:
        kind: Error type emitted by geminiClient.
        status: Optional HTTP status.
        persona: "vapls" or "indio".

    Returns:
        Localized error string for Discord.
    """
    is_indio = persona == "indio"
    if kind == "config":
        return "⚙️ Gemini no está configurado. Avisale al admin."
    if kind == "timeout":
        return "⏱️ Che, me colgué. Mandalo de nuevo." if is_indio \
            else "⏱️ Gemini tardó demasiado. Probá de nuevo."
    if kind == "http":
        if status == 429:
            base = (
                f"⏳ Me quedé sin cupo de IA por ahora. Si querés que "
                f"siga respondiendo, conseguite una key gratis en "
                f"{config.GEMINI_KEYS_DONATION_URL} (botón \"Create API key\") "
                f"y mandámela por DM al bot — la sumo al pool al toque."
            )
            credits = _format_contributors_line()
            return f"{base}\n\n{credits}" if credits else base
        return f"🌐 Algo se rompió (HTTP {status}). Probá de nuevo." if is_indio \
            else f"❌ Gemini falló (HTTP {status})."
    if kind == "blocked":
        return "🤐 No, eso no lo contesto acá. ¿Cambiamos de tema?" if is_indio \
            else "🤐 No puedo responder esto (filtros de seguridad). Reformulá."
    if kind == "empty":
        return "🤐 Eh, me quedé en blanco. Probá de nuevo." if is_indio \
            else "🤐 Gemini no devolvió texto. Probá de nuevo."
    if kind == "parse":
        return "❌ Respuesta rara de Gemini. Probá de nuevo."
    return "❌ Algo se rompió. Probá de nuevo."


async def vaplsLogic(ctx: discord.ApplicationContext, pregunta: str):
    """Handle the /vapls command using a stateless Gemini prompt.

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.

    Returns:
        None.

    Side Effects:
        Sends Discord messages and emits analytics events.

    Async:
        This function is a coroutine and must be awaited.
    """
    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=pregunta,
            system_instruction=VAPLS_SYSTEM,
            history=None,
        )
    except geminiClient.GeminiError as e:
        msg = _error_message(e.kind, e.status, "vapls")
        # Cuando es rate-limit, mostramos solo al que invocó para no
        # ensuciar el canal con texto que no aporta a la conversación.
        is_rate_limited = e.kind == "http" and e.status == 429
        try:
            await ctx.followup.send(msg, ephemeral=is_rate_limited)
        except Exception:
            pass
        analytics.capture("vapls failed", user=ctx.author, guild=ctx.guild, properties={
            "error_kind": e.kind,
            "http_status": e.status,
            "finish_reason": e.finish_reason,
            "prompt_length": len(pregunta or ""),
        })
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "vapls_generate"})
        return
    except Exception as e:
        logger.exception("vapls unexpected error")
        try:
            await ctx.followup.send("❌ Algo se rompió. Probá de nuevo.")
        except Exception:
            pass
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "vapls_unexpected"})
        return

    try:
        n_chunks = await _send_reply(ctx, _format_user_header(ctx, pregunta) + reply.text)
    except Exception as e:
        logger.exception("vapls send failed")
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "vapls_send"})
        return

    analytics.capture("vapls invoked", user=ctx.author, guild=ctx.guild, properties={
        "prompt_length": len(pregunta or ""),
        "response_length": len(reply.text),
        "response_chunks": n_chunks,
        "finish_reason": reply.finish_reason,
        "prompt_tokens": reply.prompt_tokens,
        "response_tokens": reply.response_tokens,
        "model": reply.model,
        "latency_ms": int((time.monotonic() - t0) * 1000),
    })


async def indioLogic(ctx: discord.ApplicationContext, pregunta: str, nuevo: bool):
    """Handle the /indio command with short-term conversation memory.

    Args:
        ctx: Discord application context.
        pregunta: User prompt text.
        nuevo: Whether to reset the conversation history.

    Returns:
        None.

    Side Effects:
        Updates in-memory history, sends Discord messages, and emits analytics.

    Async:
        This function is a coroutine and must be awaited.
    """
    _evict_stale_indio()
    mem_key = _indio_memory_key(ctx)
    lock = _indio_locks.setdefault(mem_key, asyncio.Lock())
    speaker = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", "alguien")
    tagged_message = f"[{speaker}]: {pregunta or ''}"

    # If this requester has a pending "¿cuál querés?" music choice, interpret
    # the message as the selection instead of a brand-new turn. We key by the
    # Discord user id (unique) so a shared display name can't cross wires.
    _choice_guild_id = getattr(getattr(ctx, "guild", None), "id", None)
    _choice_identity_val = _choice_identity(
        getattr(getattr(ctx, "author", None), "id", None) or 0, speaker)
    if not nuevo and _choice_guild_id is not None:
        async def _post_choice(text):
            channel_id = getattr(ctx, "channel_id", None) or getattr(
                getattr(ctx, "channel", None), "id", None)
            relayed = False
            if (channel_id is not None
                    and config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
                relayed = await _relay_to_userbot(channel_id, text, None)
            if not relayed:
                await _send_reply(ctx, text)

        if await _resolve_pending_music(
                ctx.bot, mem_key, _choice_identity_val, _choice_guild_id,
                pregunta or "", _post_choice):
            return

    async with lock:
        history_reset = False
        if nuevo:
            had_state = bool(_indio_history.get(mem_key)) or bool(_indio_long_term.get(mem_key))
            _indio_history.pop(mem_key, None)
            _indio_last_seen.pop(mem_key, None)
            _indio_long_term.pop(mem_key, None)
            if had_state:
                history_reset = True
                analytics.capture("indio history reset", user=ctx.author, guild=ctx.guild,
                                  properties={"trigger": "nuevo_param", "scope": "guild"})
        history_snapshot = list(_indio_history.get(mem_key, []))
        long_term_snapshot = dict(_indio_long_term.get(mem_key, {}))
    if history_reset:
        await _persist_indio_state()

    guild_for_extras = getattr(ctx, "guild", None)
    guild_id = getattr(guild_for_extras, "id", None)
    # Lazy daily refresh of the Main characters roster into persistent memory.
    await _maybe_refresh_current_members(mem_key, guild_id)
    current_members = list(_indio_current_members.get(mem_key, []))
    lt_block = _format_long_term(long_term_snapshot, current_members)
    emoji_count = len(getattr(guild_for_extras, "emojis", None) or [])
    emoji_block = _format_guild_emojis(guild_for_extras)
    logger.info("indio: roster=%d, lt_users=%d, emojis=%d (mem_key=%s)",
                len(current_members),
                len((long_term_snapshot.get("users") or {})),
                emoji_count, mem_key)
    extras = "\n\n".join(b for b in (lt_block, emoji_block) if b)
    system_instruction = INDIO_SYSTEM + (f"\n\n{extras}" if extras else "")

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=history_snapshot,
            tools=_INDIO_TOOLS,
        )
    except geminiClient.GeminiError as e:
        msg = _error_message(e.kind, e.status, "indio")
        is_rate_limited = e.kind == "http" and e.status == 429
        try:
            if is_rate_limited:
                # Posteamos el aviso visible para todos via el userbot (cuando
                # esta disponible) para que el indio "real" sea quien dice que
                # se quedo sin cupo. Header primero, para dar contexto.
                header = _format_user_header(ctx, pregunta).rstrip()
                await ctx.followup.send(header)
                channel_id = getattr(ctx, "channel_id", None) or getattr(
                    getattr(ctx, "channel", None), "id", None
                )
                relayed = False
                if (channel_id is not None
                        and config.INDIO_RELAY_URL
                        and config.INDIO_RELAY_SECRET):
                    relayed = await _relay_to_userbot(channel_id, msg, None)
                if not relayed:
                    await ctx.followup.send(msg)
            else:
                await ctx.followup.send(msg)
        except Exception:
            pass
        analytics.capture("indio failed", user=ctx.author, guild=ctx.guild, properties={
            "error_kind": e.kind,
            "http_status": e.status,
            "finish_reason": e.finish_reason,
            "prompt_length": len(pregunta or ""),
            "history_size_before": len(history_snapshot),
            "nuevo": nuevo,
        })
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "indio_generate"})
        return
    except Exception as e:
        logger.exception("indio unexpected error")
        try:
            await ctx.followup.send("❌ Algo se rompió. Probá de nuevo.")
        except Exception:
            pass
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "indio_unexpected"})
        return

    pending_actions = _actions_from_function_calls(reply.function_calls)
    pending_actions, clean_reply = await _maybe_disambiguate_music(
        ctx.bot, _choice_guild_id, mem_key, _choice_identity_val,
        pending_actions, reply,
    )
    relayed_via_userbot = False
    try:
        question_header = _format_user_header(ctx, pregunta).rstrip()
        question_msg = await ctx.followup.send(question_header)
        question_msg_id = getattr(question_msg, "id", None)
        channel_id = getattr(ctx, "channel_id", None) or getattr(
            getattr(ctx, "channel", None), "id", None
        )
        if channel_id is not None and config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            relayed_via_userbot = await _relay_to_userbot(
                channel_id, clean_reply, question_msg_id
            )
        if relayed_via_userbot:
            n_chunks = 1
        else:
            # Fallback: post the reply via vapls if relay is disabled or failed.
            n_chunks = await _send_reply(ctx, clean_reply)
    except Exception as e:
        logger.exception("indio send failed")
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "indio_send"})
        return

    if pending_actions:
        asyncio.create_task(_dispatch_indio_actions(
            ctx.bot, getattr(ctx.guild, "id", None), pending_actions,
        ))

    user_turn = {"role": "user", "parts": [{"text": tagged_message[:_STORED_MSG_MAX_CHARS]}]}
    model_turn = {"role": "model", "parts": [{"text": clean_reply[:_STORED_MSG_MAX_CHARS]}]}
    async with lock:
        existing = _indio_history.get(mem_key, history_snapshot)
        new_hist = list(existing) + [user_turn, model_turn]
        # Hard cap as a safety net if compression keeps failing.
        if len(new_hist) > _HISTORY_HARD_CAP:
            new_hist = new_hist[-_HISTORY_HARD_CAP:]
        _indio_history[mem_key] = new_hist
        _indio_last_seen[mem_key] = time.time()
        history_size_after = len(new_hist)
    await _persist_indio_state()

    # Background distillation when the short-term log grows past threshold.
    if history_size_after >= _HISTORY_COMPRESS_THRESHOLD:
        asyncio.create_task(_maybe_compress(mem_key))

    analytics.capture("indio invoked", user=ctx.author, guild=ctx.guild, properties={
        "prompt_length": len(pregunta or ""),
        "response_length": len(reply.text),
        "response_chunks": n_chunks,
        "finish_reason": reply.finish_reason,
        "prompt_tokens": reply.prompt_tokens,
        "response_tokens": reply.response_tokens,
        "model": reply.model,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "history_size_before": len(history_snapshot),
        "history_size_after": history_size_after,
        "long_term_users": len(long_term_snapshot.get("users", {}) or {}),
        "nuevo": nuevo,
        "relayed_via_userbot": relayed_via_userbot,
    })


async def indioFromVoice(
    bot: "discord.Bot",
    *,
    user_id: int,
    guild_id: int,
    channel_id: int,
    pregunta: str,
    speaker_name: Optional[str] = None,
) -> None:
    """Trigger the indio persona from a voice transcription.

    Behaves like indioLogic but without an ApplicationContext: resolves the
    guild/channel directly from the bot and posts the reply via channel.send.
    Shares the same per-guild memory bucket (_indio_memory_key returns
    "guild-<id>") so voice + slash invocations build on the same history.
    """
    pregunta = (pregunta or "").strip()
    if not pregunta:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        logger.warning("indioFromVoice: guild %s not found", guild_id)
        return
    channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
    if channel is None or not hasattr(channel, "send"):
        logger.warning("indioFromVoice: channel %s not found", channel_id)
        return
    member = guild.get_member(user_id)
    speaker = (speaker_name
               or (member.display_name if member else None)
               or "alguien")

    _evict_stale_indio()
    mem_key = f"guild-{guild_id}"
    lock = _indio_locks.setdefault(mem_key, asyncio.Lock())
    tagged_message = f"[{speaker}]: {pregunta}"
    # Key the pending choice by the Discord user id (propagated from the
    # userbot), falling back to the name only when no id is available.
    _choice_identity_val = _choice_identity(user_id, speaker)

    # Pending "¿cuál querés?" music choice for this requester → resolve it here
    # instead of starting a fresh turn.
    async def _post_choice(text):
        relayed = False
        if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            relayed = await _relay_to_userbot(channel_id, text, None)
        if not relayed:
            for chunk in _split_for_discord(text):
                await channel.send(chunk)

    if await _resolve_pending_music(
            bot, mem_key, _choice_identity_val, guild_id, pregunta, _post_choice):
        return

    async with lock:
        history_snapshot = list(_indio_history.get(mem_key, []))
        long_term_snapshot = dict(_indio_long_term.get(mem_key, {}))

    await _maybe_refresh_current_members(mem_key, guild_id)
    current_members = list(_indio_current_members.get(mem_key, []))
    lt_block = _format_long_term(long_term_snapshot, current_members)
    emoji_block = _format_guild_emojis(guild)
    extras = "\n\n".join(b for b in (lt_block, emoji_block) if b)
    system_instruction = INDIO_SYSTEM + (f"\n\n{extras}" if extras else "")

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=history_snapshot,
            tools=_INDIO_TOOLS,
        )
    except geminiClient.GeminiError as e:
        # Posteamos el aviso (incluido el 429 "conseguite una key") via el
        # userbot cuando esta disponible, asi el "Indio real" es quien dice
        # que se quedo sin cupo. Fallback a channel.send con la identidad
        # del bot vapls.
        msg = _error_message(e.kind, e.status, "indio")
        try:
            relayed = False
            if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
                relayed = await _relay_to_userbot(channel_id, msg, None)
            if not relayed:
                await channel.send(msg)
        except Exception:
            logger.exception("indioFromVoice error-send failed")
        analytics.capture("indio voice failed", user=member, guild=guild, properties={
            "error_kind": e.kind,
            "http_status": e.status,
            "prompt_length": len(pregunta),
        })
        return
    except Exception as e:
        logger.exception("indioFromVoice unexpected error")
        try:
            await channel.send("❌ Algo se rompió. Probá de nuevo.")
        except Exception:
            pass
        analytics.capture_exception(e, user=member, guild=guild,
                                    properties={"action": "indio_voice_unexpected"})
        return

    pending_actions = _actions_from_function_calls(reply.function_calls)
    pending_actions, clean_reply = await _maybe_disambiguate_music(
        bot, guild_id, mem_key, _choice_identity_val, pending_actions, reply,
    )
    relayed_via_userbot = False
    try:
        if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            relayed_via_userbot = await _relay_to_userbot(
                channel_id, clean_reply, None
            )
        if not relayed_via_userbot:
            for chunk in _split_for_discord(clean_reply):
                await channel.send(chunk)
    except Exception:
        logger.exception("indioFromVoice send failed")
        return

    if pending_actions:
        asyncio.create_task(_dispatch_indio_actions(
            bot, guild_id, pending_actions,
        ))

    user_turn = {"role": "user", "parts": [{"text": tagged_message[:_STORED_MSG_MAX_CHARS]}]}
    model_turn = {"role": "model", "parts": [{"text": clean_reply[:_STORED_MSG_MAX_CHARS]}]}
    async with lock:
        existing = _indio_history.get(mem_key, history_snapshot)
        new_hist = list(existing) + [user_turn, model_turn]
        if len(new_hist) > _HISTORY_HARD_CAP:
            new_hist = new_hist[-_HISTORY_HARD_CAP:]
        _indio_history[mem_key] = new_hist
        _indio_last_seen[mem_key] = time.time()
        history_size_after = len(new_hist)
    await _persist_indio_state()

    if history_size_after >= _HISTORY_COMPRESS_THRESHOLD:
        asyncio.create_task(_maybe_compress(mem_key))

    analytics.capture("indio voice invoked", user=member, guild=guild, properties={
        "prompt_length": len(pregunta),
        "response_length": len(clean_reply),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "relayed_via_userbot": relayed_via_userbot,
        "history_size_after": history_size_after,
    })


_BOT_TESTING_CHANNEL_NAME = "bot-testing"


DECIFRAR_SYSTEM = """\
Sos un asistente que corrige transcripciones de voz a texto en español \
rioplatense. La transcripción viene de un sistema ASR (Whisper) y puede tener \
errores: palabras mal entendidas fonéticamente, repeticiones, palabras \
inventadas o partes inaudibles. Devolvé SOLO el texto corregido, lo más fiel \
posible a lo que el hablante probablemente quiso decir, en español \
rioplatense natural. Sin comillas, sin prefijos como "Texto:", sin explicar.

Reglas:
- Si la transcripción es ininteligible o vacía, devolvé exactamente la \
  palabra: BASURA
- Si hay palabras claramente fonéticas (ruido), inferí qué se quiso decir.
- No agregues información nueva.
- Mantené la intención (pregunta, exclamación, etc.) y el voseo rioplatense.
- Si el hablante invoca al "indio" o "che indio", mantené esa parte tal cual.
- Cuando piden música, mucho ojo con nombres de bandas, canciones y artistas \
  modernos: pueden sonar en spanglish o tener nombres "raros" (ej. Tussi \
  Warriors, Bizarrap, Wos, Trueno, Tiago PZK, Duki, Cazzu, Nicki Nicole, \
  Bandalos Chinos, etc.). NO los castellanices ni los inventes en español \
  ("tossiborreros", "biza arap"). Si una palabra dentro de un pedido de \
  música suena a anglicismo o nombre propio raro, dejala lo más cerca posible \
  del inglés/original (manteniendo la fonética que escuchaste). El que va a \
  buscar el tema después busca tal cual en YouTube — si lo castellanizás, no \
  lo encuentra.
"""


# Cache de resultados de decifrar para ahorrar calls a Gemini ante falsos
# positivos de wake-word que producen transcripciones repetidas (ej. ruido
# ambiente que Whisper devuelve siempre como la misma frase). La clave se
# normaliza (lower + whitespace) para captar variantes triviales. El valor es
# lo que devolvió Gemini ("" para BASURA, texto limpio para el resto).
_DECIFRAR_CACHE_MAX = 256
_decifrar_cache: "collections.OrderedDict[str, str]" = collections.OrderedDict()


def _decifrar_cache_key(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.lower()).strip()


def _decifrar_cache_get(key: str) -> Optional[str]:
    if key in _decifrar_cache:
        _decifrar_cache.move_to_end(key)
        return _decifrar_cache[key]
    return None


def _decifrar_cache_put(key: str, value: str) -> None:
    _decifrar_cache[key] = value
    _decifrar_cache.move_to_end(key)
    while len(_decifrar_cache) > _DECIFRAR_CACHE_MAX:
        _decifrar_cache.popitem(last=False)


async def decifrarTranscripcion(texto: str) -> str:
    """Run an ASR transcript through Gemini to clean phonetic errors.

    Returns the cleaned text, or "" when Gemini flags the input as BASURA
    (so callers can drop the utterance instead of forwarding noise downstream).
    Falls back to the raw text on Gemini failure.

    Results are cached per normalized input so repeated noise transcriptions
    (typical with wake-word false positives) don't burn Gemini quota.
    """
    texto = (texto or "").strip()
    if not texto:
        return ""
    cache_key = _decifrar_cache_key(texto)
    cached = _decifrar_cache_get(cache_key)
    if cached is not None:
        logger.info("decifrar: cache hit raw=%r -> %r", texto[:200], cached[:200])
        return cached
    try:
        reply = await geminiClient.generate(
            user_message=texto,
            system_instruction=DECIFRAR_SYSTEM,
            history=None,
            model=config.GEMINI_DECIFRAR_MODEL,
            max_output_tokens=256,
        )
    except Exception:
        logger.exception("decifrarTranscripcion failed")
        return texto
    out = (reply.text or "").strip().strip('"').strip("'")
    if out.upper().strip() == "BASURA":
        logger.info("decifrar: descartado como BASURA, raw=%r", texto[:200])
        _decifrar_cache_put(cache_key, "")
        return ""
    final = out or texto
    if final != texto:
        logger.info("decifrar: raw=%r -> cleaned=%r", texto[:200], final[:200])
    else:
        logger.info("decifrar: passthrough %r", texto[:200])
    _decifrar_cache_put(cache_key, final)
    return final


async def askIndio(bot: "discord.Bot",
                   text: str,
                   speaker_name: str = "alguien",
                   *,
                   guild_id: Optional[int] = None,
                   channel_id: Optional[int] = None,
                   channel_name: Optional[str] = None,
                   user_id: int = 0) -> bool:
    """Reusable entry point to talk to the indio from anywhere in the code.

    Args:
        bot: The main Discord bot client.
        text: The user's message / question to the indio.
        speaker_name: The friendly name to attribute the message to inside
            the indio's memory (so he keeps track of who said what). Defaults
            to "alguien" if you don't have a user.
        guild_id: Optional guild ID; if omitted, picks the first guild that
            has the resolved channel.
        channel_id: Optional explicit channel ID. Wins over channel_name.
        channel_name: Channel name to resolve. Defaults to "bot-testing".
        user_id: Discord user id of the speaker (0 if unknown). Used to key
            pending music choices so only the requester can resolve them.

    Behavior:
        The reply is posted via the userbot relay (the cuenta-real "Indio")
        when configured, or via the bot itself as fallback. Uses the same
        per-guild memory bucket as /indio so messages from this entry point
        feed the same history and long-term memory.

    Returns:
        True if a reply was sent, False on any failure.
    """
    if not text or not text.strip():
        return False
    target_channel_id: Optional[int] = channel_id
    target_guild_id: Optional[int] = guild_id
    if target_channel_id is None:
        target_guild_id = guild_id
        if channel_name is None:
            channel_name = _BOT_TESTING_CHANNEL_NAME
        guilds = [bot.get_guild(target_guild_id)] if target_guild_id else list(bot.guilds)
        for guild in guilds:
            if guild is None:
                continue
            chan = discord.utils.get(getattr(guild, "text_channels", []) or [],
                                     name=channel_name)
            if chan is not None:
                target_channel_id = chan.id
                target_guild_id = guild.id
                break
    if target_channel_id is None or target_guild_id is None:
        logger.warning("askIndio: could not resolve channel (guild=%s, name=%s)",
                       guild_id, channel_name)
        return False
    await indioFromVoice(
        bot,
        user_id=user_id,
        guild_id=target_guild_id,
        channel_id=target_channel_id,
        pregunta=text,
        speaker_name=speaker_name,
    )
    return True
