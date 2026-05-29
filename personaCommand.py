"""Slash command logic for /vapls and /indio.

Both commands ask Google Gemini for a reply. /vapls is stateless (no memory).
/indio keeps a short per-user conversation history so it behaves like a
recurring character of the friend group.
"""
import asyncio
import logging
import time
from typing import Optional

import discord

import analytics
import geminiClient

logger = logging.getLogger("bot.persona")

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
como "che", "boludo" usado con afecto, "posta", "una banda", "de una"). Te \
acordás de las cosas que charlaste antes con esta persona y podés hacer \
referencia a ello. Sos opinable pero no agresivo. Si la conversación se pone \
densa o trolleadora, seguís el chiste pero sin pasarte de mambo. Si te \
preguntan si sos un bot, esquivás la pregunta con humor pero no mentís de \
manera ofensiva. Mantenés respuestas cortas, como en un chat real: 1 a 3 \
oraciones la mayoría de las veces. Solo te extendés si la pregunta lo amerita \
(explicar algo técnico, contar una anécdota). Nunca rompés el personaje para \
decir "como modelo de lenguaje..." ni nada similar.
"""

_HISTORY_MAX_TURNS = 12           # 6 user + 6 model
_STORED_MSG_MAX_CHARS = 1500
_HISTORY_TTL_SEC = 6 * 3600
_DISCORD_CHUNK_LIMIT = 1990
_MAX_CHUNKS = 4

_indio_history: dict[int, list[dict]] = {}
_indio_last_seen: dict[int, float] = {}
_indio_locks: dict[int, asyncio.Lock] = {}


def _split_for_discord(text: str) -> list[str]:
    """Split text into chunks ≤_DISCORD_CHUNK_LIMIT chars, preserving line
    boundaries when possible. Capped at _MAX_CHUNKS; the last chunk is
    truncated with an ellipsis marker if the text would exceed the cap."""
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
    now = time.time()
    stale = [uid for uid, ts in _indio_last_seen.items() if now - ts > _HISTORY_TTL_SEC]
    for uid in stale:
        _indio_history.pop(uid, None)
        _indio_last_seen.pop(uid, None)
        _indio_locks.pop(uid, None)


async def _send_reply(ctx: discord.ApplicationContext, text: str) -> int:
    chunks = _split_for_discord(text)
    for c in chunks:
        await ctx.followup.send(c)
    return len(chunks)


def _format_user_header(ctx: discord.ApplicationContext, pregunta: str) -> str:
    name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", "alguien")
    lines = (pregunta or "").splitlines() or [""]
    quoted = "\n".join(f"> {ln}" for ln in lines)
    return f"**{name}** preguntó:\n{quoted}\n\n"


def _error_message(kind: str, status: Optional[int], persona: str) -> str:
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
    _evict_stale_indio()
    user_id = ctx.author.id
    lock = _indio_locks.setdefault(user_id, asyncio.Lock())

    async with lock:
        if nuevo:
            had_history = bool(_indio_history.get(user_id))
            _indio_history.pop(user_id, None)
            if had_history:
                analytics.capture("indio history reset", user=ctx.author, guild=ctx.guild,
                                  properties={"trigger": "nuevo_param"})
        history_snapshot = list(_indio_history.get(user_id, []))

    t0 = time.monotonic()
    try:
        reply = await geminiClient.generate(
            user_message=pregunta,
            system_instruction=INDIO_SYSTEM,
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

    try:
        n_chunks = await _send_reply(ctx, _format_user_header(ctx, pregunta) + reply.text)
    except Exception as e:
        logger.exception("indio send failed")
        analytics.capture_exception(e, user=ctx.author, guild=ctx.guild,
                                    properties={"action": "indio_send"})
        return

    user_turn = {"role": "user", "parts": [{"text": (pregunta or "")[:_STORED_MSG_MAX_CHARS]}]}
    model_turn = {"role": "model", "parts": [{"text": reply.text[:_STORED_MSG_MAX_CHARS]}]}
    async with lock:
        existing = _indio_history.get(user_id, history_snapshot)
        new_hist = list(existing) + [user_turn, model_turn]
        if len(new_hist) > _HISTORY_MAX_TURNS:
            new_hist = new_hist[-_HISTORY_MAX_TURNS:]
        _indio_history[user_id] = new_hist
        _indio_last_seen[user_id] = time.time()
        history_size_after = len(new_hist)

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
        "nuevo": nuevo,
    })
