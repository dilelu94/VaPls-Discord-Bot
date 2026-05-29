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
import json
import logging
import os
import re
import tempfile
import time
from typing import Optional

import aiohttp
import discord

import analytics
import config
import geminiClient

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
        if now - last_seen > _HISTORY_TTL_SEC:
            continue
        history = val.get("history", [])
        long_term = val.get("long_term") or {}
        if isinstance(history, list) and history:
            _indio_history[key] = history
            _indio_last_seen[key] = last_seen
            loaded += 1
        if isinstance(long_term, dict) and long_term:
            _indio_long_term[key] = long_term
    if loaded or _indio_long_term:
        logger.info("indio memory: loaded %d entries (long_term keys=%d) from %s",
                    loaded, len(_indio_long_term), path)


async def _persist_indio_state() -> None:
    """Atomic write of the full indio state to disk. Held under _persist_lock
    so concurrent turns don't clobber each other's writes."""
    path = config.INDIO_MEMORY_PATH
    async with _persist_lock:
        keys = set(_indio_history) | set(_indio_long_term)
        payload = {
            "entries": {
                k: {
                    "history": _indio_history.get(k, []),
                    "last_seen": _indio_last_seen.get(k, 0.0),
                    "long_term": _indio_long_term.get(k, {}),
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


def _format_long_term(lt: dict) -> str:
    """Render long-term memory as a compact Spanish block to inject into the
    indio's system instruction. Natural-language form (no JSON) so the model
    integrates it like context, not data."""
    if not lt:
        return ""
    sections: list[str] = []
    users = lt.get("users") or {}
    if isinstance(users, dict) and users:
        user_lines = ["Lo que sabés de los pibes del grupo:"]
        for name, data in users.items():
            if not isinstance(data, dict):
                continue
            traits = [str(x) for x in (data.get("traits") or []) if x]
            qs = [str(x) for x in (data.get("preguntas_tipicas") or []) if x]
            anec = [str(x) for x in (data.get("anecdotas") or []) if x]
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
    events = [str(x) for x in (lt.get("eventos_del_grupo") or []) if x]
    if events:
        sections.append("Cosas que pasaron en el grupo:\n" + "\n".join(f"- {e}" for e in events))
    jokes = [str(x) for x in (lt.get("chistes_internos") or []) if x]
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
            return "⏳ Pará pará, tantas preguntas no — esperá un toque." if is_indio \
                else "⏳ Llegamos al límite de Gemini (10 RPM / 250 día). Esperá un toque."
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
        try:
            await ctx.followup.send(msg)
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

    lt_block = _format_long_term(long_term_snapshot)
    guild_for_emojis = getattr(ctx, "guild", None)
    emoji_count = len(getattr(guild_for_emojis, "emojis", None) or [])
    emoji_block = _format_guild_emojis(guild_for_emojis)
    logger.info("indio: injecting %d guild emojis into prompt (mem_key=%s)",
                emoji_count, mem_key)
    extras = "\n\n".join(b for b in (lt_block, emoji_block) if b)
    system_instruction = INDIO_SYSTEM + (f"\n\n{extras}" if extras else "")

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=tagged_message,
            system_instruction=system_instruction,
            history=history_snapshot,
        )
    except geminiClient.GeminiError as e:
        msg = _error_message(e.kind, e.status, "indio")
        try:
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

    clean_reply = _strip_indio_prefix(reply.text)
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
