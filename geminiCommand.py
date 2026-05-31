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
import indioArchive

try:
    from users import USERS as _USERS
except Exception:
    _USERS: dict[int, dict] = {}

try:
    from users import GROUP_LORE as _GROUP_LORE
except Exception:
    _GROUP_LORE: dict[str, list[str]] = {}

try:
    from users import NON_DISCORD_MEMBERS as _NON_DISCORD_MEMBERS
except Exception:
    _NON_DISCORD_MEMBERS: list[dict] = []

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
rasgos como "nombre real: X"). Algunos tienen además apodos alternativos \
listados como "apodos: X, Y, Z". Llamalos SIEMPRE por el apodo (el principal \
o cualquiera de los alternativos) y usá el nombre real SOLO en silencio para \
inferir si es hombre o mujer y hablarle con la concordancia correcta. \
Contexto interno (NO lo expliques en el chat a menos que la charla lleve \
directo ahí): a nadie del grupo le gusta que lo llamen por el nombre real. \
Es algo que tenés en cuenta para no meter la pata, no algo que andás \
contando. \
\
Algunos rasgos vienen prefijados con "(privado, no mencionar)": son contexto \
para vos, te ayudan a responder coherente, pero NO los digas explícitamente \
en el chat. \
\
PRINCIPIO GENERAL para toda la info que tenés del grupo (rasgos, anécdotas, \
chistes internos, lo que sea): es para que RAZONES y formules respuestas \
coherentes con quién es cada uno, no para recitarla. No andes diciendo "ah \
vos sos el de Quilmes, el bombero" o "Miles el programador de Independiente" \
cada vez que te hablan — eso es robótico y queda raro. Usá esa info como \
trasfondo que tiñe tus respuestas (vocabulario, referencias, qué chistes \
hacer con quién, qué temas evitar) y mencionalas solo cuando la conversación \
lo pide naturalmente. \
\
Si el grupo te pide música/un tema/una canción NOMBRANDO qué quieren oír \
(artista, canción, género, mood), usás la tool `play_music`. Si te piden un \
audio/sonido/clip del soundpad, usás la tool `play_sound`. Si te piden \
controlar la música que ya está sonando, usás `skip_music` (saltear/cambiar), \
`pause_music` (pausar/frenar), `resume_music` (retomar lo pausado) o \
`stop_music` (parar y limpiar la cola). \
\
DISAMBIGUACIÓN CLAVE: si decís "pone play" / "dale play" / "metele play" / \
"continuá" / "resumí" / "play" SIN nombrar artista o canción, eso NUNCA es \
play_music — es resume_music cuando hay algo pausado (mirá el [Estado del \
reproductor] del prompt). play_music solo cuando hay un nombre/género que \
buscar. \
\
Una sola tool por mensaje y solo cuando te lo piden de verdad. Antes de \
llamarla — y SOLO si la vas a llamar — mandá un texto BREVE de confirmación \
("dale, va Queen", "tomá milapollo", "dale, salteo", "va, retomo") — sin \
chamuyo. Si decidís NO llamar ninguna tool (porque es una pregunta, una \
charla, o el pedido no cumple los REQUISITOS DUROS de la tool), NO digas \
"tomá", "dale va", "ahí va", "salteo", "retomo" ni nada que suene a \
confirmación de acción — eso te deja prometiendo algo que no vas a hacer. \
Respondé como charla normal y listo. Nunca digas "no puedo" o "no me anda": \
las tools andan, las usás y listo. \
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
            "Reproducir una canción/tema NUEVO en el canal de voz #sick-tunes vía "
            "el comando /play. \n"
            "REQUISITO DURO: el mensaje DEBE tener ambas cosas: (1) un verbo "
            "explícito de orden — ponete, poneme, ponela, pone, metele, "
            "mete, tirá, tirate, tirame, reproduci, reproducí, dejá, "
            "dejame, traete, queremos escuchar — Y (2) un nombre/género/mood "
            "concreto que diga QUÉ poner (artista, canción, género, palabra "
            "clave como 'tema'). 'Dale' suelto NO cuenta como verbo de "
            "orden: es muletilla ambigua que se usa para todo (asentir, "
            "pedir, animar). Solo si el 'dale' viene seguido de OTRO verbo "
            "concreto ('dale, poneme', 'dale, tirate') vale, y ahí el verbo "
            "real es el segundo. \n"
            "Si falta el verbo de orden, NO uses esta tool aunque mencionen "
            "un artista (mencionar a 'Queen' en una conversación NO significa "
            "que quieran escucharlo). Si falta el nombre concreto, tampoco "
            "(decir 'pone algo' solo, sin más, NO sirve). \n"
            "Ejemplos VÁLIDOS: 'pone Queen', 'tirate un tema de los redondos', "
            "'metele algo de jazz', 'reproduci Despacito', 'ponete un tema'. \n"
            "Ejemplos INVÁLIDOS (NO llamar play_music): 'che indio cómo va', "
            "'me encanta Queen', 'sacá esta música' (eso es stop_music), "
            "'la música está fuerte', 'qué buen tema este'. \n"
            "Si solo dicen 'play' / 'pone play' / 'dale play' / 'metele play' / "
            "'continuá' / 'resumí' SIN nombrar artista o canción, NO uses esta "
            "tool — eso es resume_music. Mirá el [Estado del reproductor] del "
            "prompt para saber si hay algo pausado."
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
            "el canal de voz. \n"
            "REQUISITO DURO: el mensaje DEBE tener ambas cosas: (1) un verbo "
            "explícito de orden — tirá, tirate, tirame, pone, poné, ponete, "
            "ponela, ponelo, mete, metele, hacé sonar, hacelo sonar, "
            "traete, queremos escuchar — Y (2) un nombre/keyword concreto "
            "del clip a reproducir. 'Dale' suelto NO cuenta como verbo de "
            "orden: es muletilla ambigua que se usa para todo (asentir, "
            "pedir, animar). Solo si 'dale' viene seguido de OTRO verbo "
            "concreto ('dale, tirate ese audio', 'dale, pone el de las "
            "risas') vale, y ahí el verbo real es el segundo. \n"
            "Si falta el verbo de orden, NO uses esta tool aunque mencionen "
            "una palabra que matchee con un clip del soundpad. Que alguien "
            "diga 'el pez' o 'milapollo' en medio de una conversación NO "
            "significa que quieran que toques ese audio — están hablando del "
            "tema. Solo cuando hay un imperativo explícito pidiendo "
            "reproducirlo, llamás esta tool. \n"
            "Si falta el nombre concreto del clip (solo dicen 'pone un audio' "
            "sin más), tampoco la uses. \n"
            "Ejemplos VÁLIDOS: 'tirá el pezpija', 'pone el de las risas', "
            "'metele milapollo', 'hacé sonar el de aplausos', 'dale, tirate "
            "ese audio'. \n"
            "Ejemplos INVÁLIDOS (NO llamar play_sound): 'che indio tenés el "
            "pez que pescó chalo?' (es una pregunta de charla, no un pedido), "
            "'qué pescado pescó el chalo?' (sigue siendo charla), 'me "
            "encantan los memes del soundpad', 'ese audio del otro día "
            "estaba bueno', 'cuál es tu meme favorito?'."
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
            "Despausar / retomar la música que estaba pausada. Usala cuando "
            "piden 'resumí', 'resume', 'continuá' / 'continua', 'dale play', "
            "'pone play', 'metele play', 'reanudá'. "
            "REGLA CLAVE: si el [Estado del reproductor] dice que hay música "
            "pausada y el usuario pide 'play' / 'pone play' / 'continuá' / "
            "'resumí' sin nombrar artista o canción, ES ESTA TOOL — no "
            "play_music. play_music es solo cuando dicen qué quieren oír."
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

# How old a turn has to be (in seconds) before we tag it with a "[hace X]"
# prefix when feeding it back to Gemini. Without this, the model has no temporal
# cue and confuses last week's "te pasé esta lista" with the current convo.
_HISTORY_AGE_TAG_THRESHOLD_SEC = 15 * 60   # 15 minutes


def _humanize_age(seconds: float) -> str:
    """Render an age-in-seconds as a Spanish short tag for the prompt.

    Used to prefix old history turns with ``[hace X]`` so Gemini knows the
    line is not part of the current exchange. Buckets are coarse on purpose —
    the model only needs to tell "now" from "ago"."""
    if seconds < 60:
        return "hace instantes"
    if seconds < 3600:
        return f"hace {int(seconds // 60)} min"
    if seconds < 86400:
        return f"hace {int(seconds // 3600)} h"
    if seconds < 86400 * 30:
        return f"hace {int(seconds // 86400)} días"
    if seconds < 86400 * 365:
        return f"hace {int(seconds // (86400 * 30))} meses"
    return "hace más de un año"


def _stamp_history_for_prompt(history: list[dict], now: float) -> list[dict]:
    """Return a copy of ``history`` where each turn old enough gets a
    ``[hace X]`` tag prepended to its text, so the model treats those lines
    as past context, not present.

    Recent turns (≤ ``_HISTORY_AGE_TAG_THRESHOLD_SEC``) pass through unchanged
    so the current exchange reads naturally. Turns without a ``ts`` field
    (legacy entries from before this feature) are treated as old.
    """
    out: list[dict] = []
    for turn in history or []:
        ts = turn.get("ts")
        if ts is None:
            age = None
        else:
            try:
                age = max(0.0, now - float(ts))
            except (TypeError, ValueError):
                age = None
        if age is not None and age < _HISTORY_AGE_TAG_THRESHOLD_SEC:
            # Recent — leave it alone.
            out.append({k: v for k, v in turn.items() if k != "ts"})
            continue
        tag = f"[{_humanize_age(age)}] " if age is not None else "[hace tiempo] "
        new_parts = []
        for part in turn.get("parts", []):
            if isinstance(part, dict) and "text" in part:
                new_parts.append({"text": tag + str(part["text"])})
            else:
                new_parts.append(part)
        out.append({"role": turn.get("role"),
                    "parts": new_parts or turn.get("parts", [])})
    return out
_indio_long_term: dict[str, dict] = {}
_indio_locks: dict[str, asyncio.Lock] = {}
_persist_lock = asyncio.Lock()
# Per-key flag: a compression task is in-flight, don't spawn another.
_indio_compressing: set[str] = set()
# "Main characters" roster persisted alongside long-term memory. Refreshed
# at most once per ``_ROSTER_REFRESH_INTERVAL_SEC``; see _maybe_refresh_current_members.
_indio_current_members: dict[str, list[str]] = {}
_indio_members_refreshed_at: dict[str, float] = {}

# Music disambiguation via group vote. When the indio is asked for a song and
# the search returns several candidates, we list them and open a short voting
# window managed by ``playCommand.MusicVote`` (shared with the /play button
# picker so there's a single vote per guild regardless of how it was invoked).
# This module only bridges input surfaces (voice + reactions on the indio's
# chat message) into that vote.
# How many candidates to offer. Kept in sync with playCommand's /play picker.
_MUSIC_CHOICE_COUNT = 5


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
    # Music votes live in playCommand.active_votes now; they own their own
    # close timer and self-cleanup. Nothing for the indio side to evict here.


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


def _format_player_state(bot, guild_id) -> str:
    """Render the current music player state as a prompt block.

    The indio needs to know whether something is paused so ambiguous requests
    like "play" / "continuá" / "metele play" route to ``resume_music`` instead
    of ``play_music`` with a junk query. Returns "" when there is no active
    player, no voice client, or the player is fully idle.
    """
    if not guild_id:
        return ""
    try:
        import playCommand
        player = playCommand.guildPlayers.get(int(guild_id))
    except Exception:
        return ""
    if player is None:
        return ""
    title = ""
    cur = getattr(player, "currentSong", None)
    if isinstance(cur, dict):
        title = str(cur.get("title") or "").strip()
    # Interrupted state lives without a vc — the bot got kicked or dropped,
    # but we kept the song and queue in memory. The indio should steer
    # ambiguous play requests to resume_music here too.
    if getattr(player, "interrupted", False) and cur is not None:
        head = (f'música INTERRUMPIDA por desconexión — "{title}"'
                if title else "música interrumpida por desconexión")
        return (
            f"[Estado del reproductor]: {head}. Si piden 'play' / "
            f"'pone play' / 'dale play' / 'metele play' / 'continuá' / "
            f"'resumí' / 'retomá' SIN nombrar artista o canción, usá "
            f"resume_music (NO play_music) — el bot va a reconectarse y "
            f"retomar desde donde quedó."
        )
    vc = getattr(player, "vc", None)
    if vc is None:
        return ""
    try:
        if vc.is_paused():
            head = f'música PAUSADA — "{title}"' if title else "música pausada"
            return (
                f"[Estado del reproductor]: {head}. Si piden 'play' / "
                f"'pone play' / 'dale play' / 'metele play' / 'continuá' / "
                f"'resumí' SIN nombrar artista o canción, usá resume_music "
                f"(NO play_music)."
            )
    except Exception:
        pass
    try:
        if vc.is_playing():
            head = f'sonando — "{title}"' if title else "hay música sonando"
            return f"[Estado del reproductor]: {head}."
    except Exception:
        pass
    return ""


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
    sources = list(_USERS.values()) + list(_NON_DISCORD_MEMBERS)
    for info in sources:
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
    sources = list(_USERS.values()) + list(_NON_DISCORD_MEMBERS)
    for info in sources:
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
- Si un usuario repite la misma información varias veces en la conversación \
  (ej: "soy de X", "te digo que soy de X", "no te olvides que soy de X"), \
  guardala UNA sola vez. No dupliques ni expandís un rasgo porque fue \
  repetido. No registres el hecho de que lo repitió como rasgo ni anécdota.
- Si un dato ya está en la memoria actual, no lo volvás a agregar aunque \
  aparezca en la conversación nueva, ni en palabras distintas.
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


_ACTION_FAILURE_MESSAGES = {
    # Status code (set by _dispatch_indio_actions) → user-facing message. The
    # indio already promised "dale, va" optimistically *before* the tool ran;
    # these messages get posted **after** the tool fails so the user finds out
    # instead of waiting forever for music that's not coming.
    "resume: not paused":
        "uh, no había nada pausado para reanudar",
    "resume: no voice channel to rejoin":
        "no hay nadie en voz al que pueda conectarme",
    "resume: nothing to resume":
        "no me acuerdo qué estaba sonando, decime qué pongo",
    "pause: not playing":
        "no estaba sonando nada, no tengo qué pausar",
}


def _failure_feedback(status: str) -> Optional[str]:
    """Translate a status string emitted by ``_dispatch_indio_actions`` into a
    user-facing apology, or ``None`` if the status was a success (no feedback
    needed). Used to surface tool failures the indio promised optimistically."""
    if not status:
        return None
    if status in _ACTION_FAILURE_MESSAGES:
        return _ACTION_FAILURE_MESSAGES[status]
    if status.endswith(": no active player"):
        return "no había reproductor activo, no estaba sonando nada"
    if status.startswith("music: fail"):
        # Extract the inner reason after " — " when present.
        _, _, reason = status.partition(" — ")
        return (f"no pude poner la música ({reason})"
                if reason else "no pude poner la música")
    if status.startswith("sound: fail"):
        _, _, reason = status.partition(" — ")
        return (f"no encontré el sonido ({reason})"
                if reason else "no encontré ese sonido")
    if status.startswith("resume: reconnect failed"):
        return "no pude reconectarme al canal para retomar la música"
    return None


async def _post_action_failures(channel_id: Optional[int], channel,
                                statuses: list[str]) -> None:
    """Post one user-facing message per failed action status. Best-effort:
    relays via the userbot (so the indio "owns" the apology) and falls back to
    ``channel.send`` when the relay is off. ``channel`` may be ``None`` —
    in that case we only attempt the relay path."""
    seen: set[str] = set()
    for status in statuses or []:
        msg = _failure_feedback(status)
        if not msg or msg in seen:
            continue
        seen.add(msg)
        logger.info("indio action feedback: %r → %r", status, msg)
        relayed = False
        if channel_id and config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            try:
                relayed = await _relay_to_userbot(int(channel_id), msg, None)
            except Exception:
                logger.exception("indio action feedback relay failed")
                relayed = False
        if not relayed and channel is not None:
            try:
                await channel.send(msg)
            except Exception:
                logger.exception("indio action feedback channel.send failed")


async def _dispatch_indio_actions(bot: "discord.Bot",
                                   guild_id: Optional[int],
                                   actions: list[tuple[str, str]],
                                   feedback_channel_id: Optional[int] = None,
                                   feedback_channel=None,
                                   ) -> list[str]:
    """Run any PLAY_* actions the indio emitted. Both PLAY_MUSIC and
    PLAY_SOUND are invoked through the userbot relay so they show up as
    real "/play" / "/soundpad" slash commands in the chat. Both land in
    ``config.INDIO_PLAY_CHANNEL_ID`` — that's the dedicated room for
    playback regardless of where the conversation is happening. Falls back
    to in-process playback if the relay is unavailable.

    When ``feedback_channel_id`` / ``feedback_channel`` are provided, any
    action that fails triggers a corrective message in that channel so the
    user finds out (the indio already promised "dale, va" optimistically
    before the tool ran).

    Returns short status strings for logging; the indio's main reply is sent
    separately."""
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
                    elif getattr(player, "interrupted", False) and player.currentSong:
                        # Bot was kicked / lost connection while a song was
                        # playing. Reconnect to the most-populated voice
                        # channel and pick up where we left off.
                        try:
                            voice_channel = playCommand._pick_voice_channel(
                                bot, int(guild_id),
                            )
                        except Exception:
                            voice_channel = None
                        if voice_channel is None:
                            statuses.append("resume: no voice channel to rejoin")
                        else:
                            try:
                                new_vc = await voice_channel.connect(reconnect=True)
                                resumed = await player.resumeFromInterruption(new_vc)
                                if resumed:
                                    statuses.append("resume: reconnected & resumed")
                                    control_ok = True
                                else:
                                    statuses.append("resume: nothing to resume")
                            except Exception as e:
                                logger.exception("indio RESUME_MUSIC reconnect failed")
                                statuses.append(f"resume: reconnect failed ({e})")
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
            statuses.append(f"{action.lower()}: fail — exception")
    # After all actions ran, push corrective messages for any failures so the
    # user gets actual feedback instead of being left waiting on the optimistic
    # "dale, va" the indio already posted.
    try:
        await _post_action_failures(feedback_channel_id, feedback_channel, statuses)
    except Exception:
        logger.exception("indio action feedback dispatch failed")
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


# Words that, when adjacent to an ordinal/number token, signal "this is a
# selection". Required for ordinal-word matching so bare "uno" / "dos" in
# normal speech doesn't get parsed as a vote. Includes the selection article
# ("la 4"), imperatives ("ponela 2", "elegí la tres", "votá la una"), and the
# explicit "opción"/"número" framing.
# Stored without accents — _parse_choice normalizes input the same way via
# _normalize_choice, so the lookup just needs the accent-stripped form.
_SELECTION_CONTEXT_WORDS = {
    "la", "el", "los", "las",
    "ponela", "ponelo", "poneme", "ponete", "pone",
    "metele", "mete", "tirate", "tira", "tirame",
    "dame", "dale", "elegi", "elegime", "elige",
    "vota", "voto", "votala", "votalo",
    "quiero", "ese", "esa", "este", "esta",
    "opcion", "numero", "n",
}


def _parse_choice(text: str, candidates: list[dict]):
    """Interpret a selection utterance against the offered candidates.

    Returns the 0-based index of the chosen candidate, the string ``"cancel"``
    when the speaker declined, or ``None`` when the message doesn't look like a
    selection at all (caller should treat it as a normal new message).

    Resolution order: explicit cancel > digit (1..N) > ordinal word
    (primera/segunda/…) **only when preceded by a selection context word** >
    a distinctive word that matches exactly one title.

    The selection-context requirement on ordinals is what stops normal speech
    ("Uno lava todo") from being parsed as a vote — there's no leading "la" /
    "ponela" / etc., so bare "uno" is ignored.
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
    for i, tok in enumerate(tokens):
        for stem, idx in _ORDINAL_STEMS.items():
            if not tok.startswith(stem) or idx >= n:
                continue
            # Only accept the ordinal if the previous token signals selection
            # intent. Falls through to None if it doesn't — the caller treats
            # this as a normal message (chat / Gemini turn), not a vote.
            prev = tokens[i - 1] if i > 0 else ""
            if prev in _SELECTION_CONTEXT_WORDS:
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


_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣",
              "5️⃣", "6️⃣", "7️⃣", "8️⃣",
              "9️⃣", "\U0001f51f"]


def _num_emoji(i: int) -> str:
    """Keycap emoji for a 1-based position (display only)."""
    return _NUM_EMOJI[i - 1] if 1 <= i <= len(_NUM_EMOJI) else f"{i})"


def _format_choices(candidates: list[dict]) -> str:
    """Render the "¿cuál querés?" list the indio posts in chat."""
    lines = ["che, ¿cuál de estas querés?"]
    for i, c in enumerate(candidates, 1):
        dur = c.get("duration_string") or ""
        durs = f" [{dur}]" if dur else ""
        lines.append(f"{_num_emoji(i)} {c['title']}{durs}")
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


def _voter_id_from(user_id, speaker: str) -> int:
    """Resolve a stable integer "voter id" for MusicVote.register_vote, which
    keys ``votes`` by int (Discord uid). Falls back to a deterministic hash of
    the speaker name when no real id is available (older voice messages)."""
    try:
        if user_id:
            return int(user_id)
    except (TypeError, ValueError):
        pass
    # Negative-space "name" voter ids so they never collide with real Discord
    # uids (which are positive).
    name = speaker or "alguien"
    return -(hash(name) & 0xFFFFFFFF) or -1


def _try_register_chat_vote(guild_id: Optional[int], user_id: int,
                            text: str) -> bool:
    """Bridge for typed-chat votes ("indio ponela 4" in text). Same idea as
    ``try_register_voice_vote`` but doesn't close immediately — typing is more
    deliberate, but we still want to give the group its sliding window."""
    if not guild_id or not text:
        return False
    import playCommand
    vote = playCommand.get_active_vote(int(guild_id))
    if vote is None:
        return False
    decision = _parse_choice(text, vote.candidates)
    if not isinstance(decision, int) or not (0 <= decision < len(vote.candidates)):
        return False
    return vote.register_vote(int(user_id), decision)


def try_register_voice_vote(*, guild_id: Optional[int], user_id: int,
                            speaker_name: str, text: str) -> bool:
    """Try to register a voice utterance as a vote on the guild's open music
    poll. Returns True when ``text`` parses as a choice and a vote is recorded;
    False when there's no open vote, no guild context, or ``text`` doesn't name
    an option.

    Voice votes are decisive: this is someone literally telling the indio "ponela
    4", so we close the vote immediately (``close_now=True``) instead of waiting
    out the sliding window. Called from the apiServer **before**
    ``decifrarTranscripcion`` so the raw transcript's digit ("Indio, tirala 4")
    survives even if Gemini's cleanup would otherwise drop it.
    """
    if not guild_id or not text:
        return False
    import playCommand
    vote = playCommand.get_active_vote(int(guild_id))
    if vote is None:
        return False
    decision = _parse_choice(text, vote.candidates)
    if not isinstance(decision, int) or not (0 <= decision < len(vote.candidates)):
        return False
    voter = _voter_id_from(user_id, speaker_name or "")
    return vote.register_vote(voter, decision, close_now=True)


def register_reaction_vote(*, channel_id: int, message_id: int,
                           emoji: str, user_id: int) -> bool:
    """Count an emoji reaction on a vote's options message as a vote.

    Called from the main bot's ``on_raw_reaction_add``. Looks up the open vote
    by its options message, maps the keycap emoji to an option, and records the
    reactor's pick keyed by user id. Reactions slide the timer (no close_now);
    the assumption is multiple people may be reacting in sequence and we want
    to give them a window.
    """
    import playCommand
    idx = playCommand.emoji_to_index((emoji or "").strip())
    if idx is None:
        # Some clients drop the variation selector — try the bare keycap too.
        idx = playCommand.emoji_to_index((emoji or "").replace("\ufe0f", ""))
    if idx is None:
        return False
    try:
        cid = int(channel_id)
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    for vote in playCommand.active_votes.values():
        if vote.closed:
            continue
        if vote.reaction_message_id == mid and vote.reaction_channel_id == cid:
            if idx >= len(vote.candidates):
                return False
            return vote.register_vote(int(user_id), idx)
    return False


async def _relay_say(channel_id: int, content: str) -> Optional[int]:
    """Post ``content`` via the userbot relay and return the first message id
    (so the main bot can react to it), or None if the relay is off/failed.
    Mirrors _relay_to_userbot but surfaces the message id."""
    url = config.INDIO_RELAY_URL
    secret = config.INDIO_RELAY_SECRET
    if not url or not secret:
        return None
    payload = {"channel_id": int(channel_id), "content": content}
    headers = {"X-API-Secret": secret}
    timeout = aiohttp.ClientTimeout(total=config.INDIO_RELAY_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json(content_type=None)
        ids = (data or {}).get("message_ids") or []
        return int(ids[0]) if ids else None
    except Exception:
        logger.exception("indio relay say (with id) failed")
        return None


async def _attach_vote_reactions(bot, vote, channel_id: int,
                                 message_id: int, n: int) -> None:
    """Remember which message carries this vote and seed it with the number
    reactions (1️⃣…N) so people can vote by reacting. Best-effort.

    Refuses to overwrite an existing binding: once a vote has been attached
    to a message, **a later turn's unrelated reply must not steal it**. That
    was the 2026-05-31 bug — a chat reply ("¡pará, Enrique!…") got 1-5
    reactions slapped on it because a music vote from an earlier turn was
    still open in the guild.
    """
    if vote is None or not channel_id or not message_id:
        return
    if vote.reaction_message_id is not None:
        # Already bound to a real options message. Don't repoint.
        return
    vote.reaction_channel_id = int(channel_id)
    vote.reaction_message_id = int(message_id)
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
        for i in range(1, min(n, len(_NUM_EMOJI)) + 1):
            await msg.add_reaction(_num_emoji(i))
    except Exception:
        logger.exception("indio vote: attaching reactions failed")


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


async def _maybe_disambiguate_music(bot, guild_id, mem_key,
                                    pending_actions, reply, post):
    """Intercept a single free-text ``play_music`` so the indio lists the
    matches and opens a group vote, instead of playing the first hit.

    The search reuses yt-dlp exactly like before (no extra Gemini). With a
    single clear hit we play it directly; with several we list them and open a
    voting window (``post`` is how the winner gets announced when it closes). A
    direct URL, several actions at once, or a non-music turn pass through
    untouched.

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
    # Re-rank by fuzzy similarity against the user's query so the option the
    # vote falls back to when nobody picks (candidates[0] in _tally_vote_winner)
    # is the one that actually best matches what was asked, not just YouTube's
    # top relevance hit. Stable for ties (Python's sort) so ratio ties keep
    # YouTube's relative order. Helper lives in playCommand alongside the /play
    # autoplay logic — single source of truth for "what matches the query".
    candidates.sort(
        key=lambda c: playCommand._query_title_ratio(query, c.get("title", "")),
        reverse=True,
    )
    if len(candidates) == 1:
        # One clear match: play it directly with the metadata we already have.
        asyncio.create_task(_play_chosen_song(bot, guild_id, candidates[0]))
        return [], clean
    # Several matches: open a shared MusicVote (one per guild — same storage
    # used by the /play picker buttons). The indio's chat surface here lists
    # the options as text + reactions; voice votes and button clicks all write
    # into the same vote state.
    import playCommand

    async def _on_resolve(vote, winner: dict) -> None:
        # Announce + reproduce. ``post`` was passed in by the caller and knows
        # how to send via the userbot relay (or fall back to channel.send).
        try:
            await post(f"dale, va: {winner['title']} 🎵")
        except Exception:
            logger.exception("indio vote: announce failed")
        await _play_chosen_song(bot, guild_id, winner)

    playCommand.open_music_vote(
        bot=bot, guild_id=int(guild_id),
        candidates=candidates, on_resolve=_on_resolve,
    )
    return [], _format_choices(candidates)


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

    # How the winner gets announced when the vote closes (relay as the real
    # indio when configured, else via this command's response).
    async def _post_choice(text):
        channel_id = getattr(ctx, "channel_id", None) or getattr(
            getattr(ctx, "channel", None), "id", None)
        relayed = False
        if (channel_id is not None
                and config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET):
            relayed = await _relay_to_userbot(channel_id, text, None)
        if not relayed:
            await _send_reply(ctx, text)

    # If a music vote is open for this guild and the message names an option,
    # count it as a vote (anyone can vote) instead of a brand-new turn. Keyed by
    # the Discord user id so each person gets one vote.
    _choice_guild_id = getattr(getattr(ctx, "guild", None), "id", None)
    _choice_identity_val = _choice_identity(
        getattr(getattr(ctx, "author", None), "id", None) or 0, speaker)
    if (not nuevo and _choice_guild_id is not None
            and _try_register_chat_vote(
                int(_choice_guild_id),
                int(getattr(getattr(ctx, "author", None), "id", None) or 0),
                pregunta or "",
            )):
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
    player_block = _format_player_state(getattr(ctx, "bot", None), guild_id)
    logger.info("indio: roster=%d, lt_users=%d, emojis=%d (mem_key=%s)",
                len(current_members),
                len((long_term_snapshot.get("users") or {})),
                emoji_count, mem_key)
    extras = "\n\n".join(b for b in (lt_block, emoji_block, player_block) if b)
    system_instruction = INDIO_SYSTEM + (f"\n\n{extras}" if extras else "")

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=_stamp_history_for_prompt(history_snapshot, time.time()),
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
        ctx.bot, _choice_guild_id, mem_key, pending_actions, reply, _post_choice,
    )
    relayed_via_userbot = False
    import playCommand
    _active_vote = playCommand.get_active_vote(int(getattr(ctx.guild, "id", 0) or 0))
    # "vote_open" here means "this turn just opened a vote and the reply IS
    # the options listing". A vote that already has a reaction_message_id
    # belongs to a previous turn — don't treat the current reply as its
    # surface (otherwise unrelated chat replies get 1-5 reactions slapped on).
    vote_open = (_active_vote is not None
                 and _active_vote.reaction_message_id is None)
    opts_channel_id = None
    opts_msg_id = None
    try:
        question_header = _format_user_header(ctx, pregunta).rstrip()
        question_msg = await ctx.followup.send(question_header)
        question_msg_id = getattr(question_msg, "id", None)
        channel_id = getattr(ctx, "channel_id", None) or getattr(
            getattr(ctx, "channel", None), "id", None
        )
        opts_channel_id = channel_id
        if vote_open and config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET and channel_id is not None:
            # Vote options: post via relay but capture the message id so we can
            # add the number reactions to it.
            opts_msg_id = await _relay_say(channel_id, clean_reply)
            relayed_via_userbot = opts_msg_id is not None
            n_chunks = 1 if relayed_via_userbot else 0
            if not relayed_via_userbot:
                sent = await ctx.followup.send(clean_reply)
                opts_msg_id = getattr(sent, "id", None)
                opts_channel_id = getattr(getattr(sent, "channel", None), "id", None) or channel_id
                n_chunks = 1
        elif vote_open:
            sent = await ctx.followup.send(clean_reply)
            opts_msg_id = getattr(sent, "id", None)
            opts_channel_id = getattr(getattr(sent, "channel", None), "id", None) or channel_id
            n_chunks = 1
        elif channel_id is not None and config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            relayed_via_userbot = await _relay_to_userbot(
                channel_id, clean_reply, question_msg_id
            )
            n_chunks = 1 if relayed_via_userbot else await _send_reply(ctx, clean_reply)
        else:
            # Fallback: post the reply via vapls if relay is disabled or failed.
            n_chunks = await _send_reply(ctx, clean_reply)
    except Exception as e:
        logger.exception("indio send failed")
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "indio_send"})
        return

    if vote_open and opts_msg_id and opts_channel_id and _active_vote is not None:
        n = len(_active_vote.candidates)
        await _attach_vote_reactions(
            ctx.bot, _active_vote, opts_channel_id, opts_msg_id, n,
        )

    try:
        await indioArchive.enqueue(
            guild_id=getattr(getattr(ctx, "guild", None), "id", None),
            channel_id=channel_id,
            speaker=speaker,
            question=pregunta or "",
            reply=clean_reply,
        )
    except Exception:
        logger.exception("indio archive enqueue failed")

    if pending_actions:
        _fb_channel_id = getattr(ctx, "channel_id", None) or getattr(
            getattr(ctx, "channel", None), "id", None
        )
        asyncio.create_task(_dispatch_indio_actions(
            ctx.bot, getattr(ctx.guild, "id", None), pending_actions,
            feedback_channel_id=_fb_channel_id,
            feedback_channel=getattr(ctx, "channel", None),
        ))

    _turn_ts = time.time()
    user_turn = {"role": "user", "parts": [{"text": tagged_message[:_STORED_MSG_MAX_CHARS]}], "ts": _turn_ts}
    model_turn = {"role": "model", "parts": [{"text": clean_reply[:_STORED_MSG_MAX_CHARS]}], "ts": _turn_ts}
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

    # How the winner gets announced when the vote closes.
    async def _post_choice(text):
        relayed = False
        if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
            relayed = await _relay_to_userbot(channel_id, text, None)
        if not relayed:
            for chunk in _split_for_discord(text):
                await channel.send(chunk)

    # If a music vote is open and this message names an option, count it as a
    # vote (anyone can vote) instead of starting a fresh turn. This is the
    # voice path — treated as decisive (close_now=True) since the user just
    # spoke the choice out loud.
    if try_register_voice_vote(guild_id=guild_id, user_id=user_id,
                               speaker_name=speaker, text=pregunta):
        return

    async with lock:
        history_snapshot = list(_indio_history.get(mem_key, []))
        long_term_snapshot = dict(_indio_long_term.get(mem_key, {}))

    await _maybe_refresh_current_members(mem_key, guild_id)
    current_members = list(_indio_current_members.get(mem_key, []))
    lt_block = _format_long_term(long_term_snapshot, current_members)
    emoji_block = _format_guild_emojis(guild)
    player_block = _format_player_state(bot, guild_id)
    extras = "\n\n".join(b for b in (lt_block, emoji_block, player_block) if b)
    system_instruction = INDIO_SYSTEM + (f"\n\n{extras}" if extras else "")

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=_stamp_history_for_prompt(history_snapshot, time.time()),
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
        bot, guild_id, mem_key, pending_actions, reply, _post_choice,
    )
    relayed_via_userbot = False
    import playCommand
    _active_vote = playCommand.get_active_vote(int(guild_id) if guild_id else 0)
    # Same gate as indioLogic: only treat this turn's reply as the options
    # surface when the live vote is the one we just opened (no message bound
    # yet). Avoids the "unrelated chat reply gets 1-5 reactions" bug.
    vote_open = (_active_vote is not None
                 and _active_vote.reaction_message_id is None)
    opts_msg_id = None
    try:
        if vote_open:
            # Vote options: capture the message id so we can react on it.
            if config.INDIO_RELAY_URL and config.INDIO_RELAY_SECRET:
                opts_msg_id = await _relay_say(channel_id, clean_reply)
                relayed_via_userbot = opts_msg_id is not None
            if not relayed_via_userbot:
                sent = None
                for chunk in _split_for_discord(clean_reply):
                    sent = await channel.send(chunk)
                opts_msg_id = getattr(sent, "id", None)
        else:
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

    if vote_open and opts_msg_id and _active_vote is not None:
        n = len(_active_vote.candidates)
        await _attach_vote_reactions(bot, _active_vote, channel_id, opts_msg_id, n)

    try:
        await indioArchive.enqueue(
            guild_id=guild_id,
            channel_id=channel_id,
            speaker=speaker,
            question=pregunta,
            reply=clean_reply,
        )
    except Exception:
        logger.exception("indioFromVoice archive enqueue failed")

    if pending_actions:
        asyncio.create_task(_dispatch_indio_actions(
            bot, guild_id, pending_actions,
            feedback_channel_id=channel_id,
            feedback_channel=channel,
        ))

    _turn_ts = time.time()
    user_turn = {"role": "user", "parts": [{"text": tagged_message[:_STORED_MSG_MAX_CHARS]}], "ts": _turn_ts}
    model_turn = {"role": "model", "parts": [{"text": clean_reply[:_STORED_MSG_MAX_CHARS]}], "ts": _turn_ts}
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
- **PRESERVÁ NÚMEROS LITERALES**: si el ASR transcribió un dígito ("4", "2"), \
  ese dígito DEBE aparecer en la corrección. Nunca lo borres ni lo reemplaces \
  por palabras. Ej.: "Indio, tiradela 4" → "che indio, tiradela 4" (NO \
  "che indio, tirala"). "Indio ponela 3" → "che indio, ponela 3". El número \
  es la respuesta a una votación abierta — si lo perdés, el voto no cuenta.
- **PRESERVÁ EL MODO IMPERATIVO**: si el hablante da una orden ("tirate", \
  "ponete", "ponela", "dale play", "tirá"), DEBE seguir siendo imperativo en \
  la salida. Nunca lo conjugues en pasado ni en otro modo. Ej.: "Tírate la 4" \
  → "tirate la 4" (NO "tiraste la 4"). "Pone música" → "poné música" (NO \
  "puse música"). El indio recibe órdenes, no narraciones.
- Cuando piden música, mucho ojo con nombres de bandas, canciones y artistas \
  modernos: pueden sonar en spanglish o tener nombres "raros" (ej. Tussi \
  Warriors, Bizarrap, Wos, Trueno, Tiago PZK, Duki, Cazzu, Nicki Nicole, \
  Bandalos Chinos, etc.). NO los castellanices ni los inventes en español \
  ("tossiborreros", "biza arap"). Si una palabra dentro de un pedido de \
  música suena a anglicismo o nombre propio raro, dejala lo más cerca posible \
  del inglés/original (manteniendo la fonética que escuchaste). El que va a \
  buscar el tema después busca tal cual en YouTube — si lo castellanizás, no \
  lo encuentra.

Ejemplos (raw → corregido). Whisper parte verbos imperativos rioplatenses \
en pedazos cuando vienen pegados al wake-word; reconstruí el verbo canónico \
del comando que pidió el hablante:
- "Indio de tener a música" → "che indio, detené la música"
- "indio para la música" → "che indio, pará la música"
- "che indio corta la" → "che indio, cortala"
- "indio pone Bizarrap" → "che indio, poné Bizarrap"
- "indio tirate algo de Wos" → "che indio, tirate algo de Wos"
- "indio passa al siguiente" → "che indio, pasá al siguiente"
- "indio saltea esta" → "che indio, saltá esta"
- "indio pausa un toque" → "che indio, pausá un toque"
- "indio segui con la música" → "che indio, seguí con la música"
- "indio dale continua" → "che indio, dale, continuá"
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


# Conocidas-y-recurrentes phonetic confusions that Whisper hace en castellano
# rioplatense. Aplicado como substring substitution case-insensitive ANTES de
# pasar el texto a Gemini, así pedidos tipo "ponete un tema de líneas horarias"
# se corrigen a "ponete un tema de indio solari" sin necesidad de votación
# previa. Cualquier match curado por el voting (👍) termina acá también, pero
# este set es la "memoria base" que no se aprende sola — la armamos a mano
# cuando vemos un error recurrente.
#
# Convención: claves todas en lower-case (la sustitución es case-insensitive).
# Si el target es un nombre propio que YouTube necesita con caps, el valor lo
# lleva en su forma canónica.
_KNOWN_PHONETIC_FIXES: "list[tuple[str, str]]" = [
    # Indio Solari — Whisper se la come y la pasa a "líneas horarias" /
    # "lineas orarias" / similares. Ver caso 2026-05-31 en logs.
    ("líneas horarias", "Indio Solari"),
    ("lineas horarias", "Indio Solari"),
    ("líneas orarias", "Indio Solari"),
    ("lineas orarias", "Indio Solari"),
    ("indio sorari", "Indio Solari"),
    ("indio sorare", "Indio Solari"),
    ("indio solare", "Indio Solari"),
]


def _apply_known_fixes(texto: str) -> str:
    """Apply the manually-curated phonetic-confusion table to a transcript.

    Case-insensitive substring substitution; order matters when one fix is a
    prefix of another so we apply them in declaration order. Returns the input
    unchanged if no fix matches.
    """
    if not texto:
        return texto
    out = texto
    for bad, good in _KNOWN_PHONETIC_FIXES:
        if not bad:
            continue
        # Case-insensitive replace via regex with re.escape.
        out = re.sub(re.escape(bad), good, out, flags=re.IGNORECASE)
    return out


def seed_decifrar_cache(items: "list[tuple[str, str]]") -> None:
    """Bulk-insert pre-normalized (key, value) pairs into the in-memory
    decifrar cache. Used by ``decifrarVoting`` to hydrate the cache at
    startup (from approved JSONL entries) and to promote freshly-approved
    entries at runtime. Items go through ``_decifrar_cache_put`` so they
    obey the LRU cap and get marked as "recent" on insertion.
    """
    for key, value in items:
        if not key:
            continue
        _decifrar_cache_put(key, value)


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
    # Aplicar correcciones manuales (substring) ANTES del cache lookup: así
    # el caché guarda directamente la versión ya corregida, y un mismo error
    # recurrente comparte hit aun cuando venga con variantes de mayúsculas.
    texto = _apply_known_fixes(texto)
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
    # Fire-and-forget: log the pair for the human-in-the-loop curation flow.
    # Late import keeps this module standalone if decifrarVoting isn't wired
    # (e.g. tests of geminiCommand alone).
    try:
        import decifrarVoting
        asyncio.create_task(decifrarVoting.record(texto, final))
    except Exception:
        logger.exception("decifrar: failed to schedule voting record")
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
