"""User suggestions system — `/sugerencias` and `/sugerencias-ver`.

Modela las ideas de feature/cambios de los usuarios como un sistema con su
propio modelo de datos (``Submission`` / ``Group``) y un store persistente
(``SuggestionStore``). El flujo:

1. ``/sugerencias <idea>`` pasa la idea por Gemini Flash-Lite, que decide si
   encaja en un grupo existente o abre uno nuevo. **Solo se persiste si Gemini
   logró categorizar** — si falla, no se guarda nada y se le pide al usuario que
   reintente (categorizado-o-nada). Cada idea categorizada se registra además
   como evento en PostHog.
2. ``/sugerencias-ver`` lista los grupos ordenados por cuántas personas pidieron
   cada cosa, para que la gente sepa qué ideas ya existen.

Formato persistido (``config.SUGGESTIONS_PATH``)::

    {
      "groups": [
        {
          "id": "g_<uuid4-short>",
          "title": "Comando para X",
          "summary": "Que el bot haga Y cuando Z",
          "created_at": "2026-05-30T12:34:56Z",
          "updated_at": "2026-05-30T12:34:56Z",
          "submissions": [
            {
              "user_id": "123",
              "user_name": "Mati",
              "text": "podrias agregar Y para que pase Z",
              "at": "2026-05-30T12:34:56Z"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import analytics
import config
import geminiClient

logger = logging.getLogger("bot.suggestions")

_MAX_IDEA_CHARS = 1000
_MAX_TITLE_CHARS = 80
_MAX_SUMMARY_CHARS = 240
_VER_MAX_GROUPS = 10
_PERSIST_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _new_group_id() -> str:
    return f"g_{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Submission:
    """A single user's request, as raw text plus who/when."""

    user_id: str
    user_name: str
    text: str
    at: str

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "text": self.text,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Submission":
        return cls(
            user_id=str(d.get("user_id", "")),
            user_name=str(d.get("user_name", "anon")),
            text=str(d.get("text", "")),
            at=str(d.get("at", "")) or _now_iso(),
        )


@dataclass
class Group:
    """A cluster of submissions pointing at the same feature/idea."""

    id: str
    title: str
    summary: str
    created_at: str
    updated_at: str
    submissions: list[Submission] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.submissions)

    def add(self, sub: Submission) -> None:
        self.submissions.append(sub)
        self.updated_at = sub.at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "submissions": [s.to_dict() for s in self.submissions],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Group":
        subs = d.get("submissions")
        return cls(
            id=str(d.get("id") or _new_group_id()),
            title=str(d.get("title", "")),
            summary=str(d.get("summary", "")),
            created_at=str(d.get("created_at", "")) or _now_iso(),
            updated_at=str(d.get("updated_at", "")) or _now_iso(),
            submissions=[Submission.from_dict(s) for s in subs]
            if isinstance(subs, list)
            else [],
        )


def _write_json_atomic(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".suggestions_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as e:
            logger.warning("Failed to remove temp file %s: %s", tmp, e)
        raise


class SuggestionStore:
    """Reads/writes the suggestions JSON, returning ``Group`` objects.

    The on-disk shape is the legacy ``{"groups": [...]}`` document so existing
    files keep working. Writes are atomic and serialized through a process-wide
    lock to avoid clobbering concurrent submissions.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> list[Group]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except Exception:
            logger.exception("suggestions load failed at %s", self.path)
            return []
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            return []
        return [Group.from_dict(g) for g in groups if isinstance(g, dict)]

    async def save(self, groups: list[Group]) -> None:
        payload = {"groups": [g.to_dict() for g in groups]}
        async with _PERSIST_LOCK:
            try:
                await asyncio.to_thread(_write_json_atomic, self.path, payload)
            except Exception:
                logger.exception("suggestions persist failed at %s", self.path)


def _store() -> SuggestionStore:
    # Resolve the path lazily so tests can monkeypatch config.SUGGESTIONS_PATH.
    return SuggestionStore(config.SUGGESTIONS_PATH)


# --------------------------------------------------------------------------
# Classification (Gemini Flash-Lite)
# --------------------------------------------------------------------------
_CLASSIFY_SYSTEM = """\
Sos un clasificador de sugerencias de usuarios para un bot de Discord.
Recibis una sugerencia nueva y la lista de grupos existentes (cada uno con id,
titulo y resumen). Decidis si la nueva sugerencia encaja en alguno de los
grupos existentes o si abre un grupo nuevo.

Devolves SOLO un JSON con esta forma exacta, sin markdown ni explicacion:

  Si encaja en un grupo existente:
    {"match_id": "<id del grupo>"}

  Si es una idea nueva:
    {"new_title": "<titulo corto, <=80 chars>", "new_summary": "<resumen 1-2 frases, <=240 chars>"}

Reglas:
- "Encaja" significa que apunta al mismo cambio/feature, no solo al mismo tema.
- Si dudas entre encajar y crear, preferi crear: es mas facil mergear despues
  que separar.
- El titulo y resumen van en castellano rioplatense, sin emojis, sin comillas.
- Si la idea es ambigua, intenta capturar la intencion general en el resumen.
"""


def _classify_prompt(idea: str, groups: list[Group]) -> str:
    if groups:
        lines = ["Grupos existentes:"]
        for g in groups:
            lines.append(f"- id={g.id} | titulo={g.title} | resumen={g.summary}")
        existing = "\n".join(lines)
    else:
        existing = "Grupos existentes: (ninguno todavia)"
    return f"{existing}\n\nSugerencia nueva:\n{idea}"


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


@dataclass
class Classification:
    match_id: Optional[str] = None
    new_title: Optional[str] = None
    new_summary: Optional[str] = None


async def _classify(idea: str, groups: list[Group]) -> Optional[Classification]:
    """Run Gemini Flash-Lite. Returns ``None`` when classification fails so the
    caller can refuse to persist (categorized-or-nothing)."""
    try:
        reply = await geminiClient.generate(
            user_message=_classify_prompt(idea, groups),
            system_instruction=_CLASSIFY_SYSTEM,
            model=config.SUGGESTIONS_MODEL,
            max_output_tokens=256,
        )
    except Exception:
        logger.exception("suggestions: gemini classify failed")
        return None
    parsed = _extract_json(reply.text or "")
    if not parsed:
        logger.warning("suggestions: classifier returned non-json: %r", reply.text)
        return None
    match_id = parsed.get("match_id")
    if isinstance(match_id, str) and match_id.strip():
        # Validate the id actually exists; otherwise treat as no-match.
        if any(g.id == match_id.strip() for g in groups):
            return Classification(match_id=match_id.strip())
        logger.warning("suggestions: classifier returned unknown match_id %r", match_id)
    title = parsed.get("new_title")
    summary = parsed.get("new_summary")
    if isinstance(title, str) and title.strip():
        return Classification(
            new_title=title.strip()[:_MAX_TITLE_CHARS],
            new_summary=(
                summary.strip()[:_MAX_SUMMARY_CHARS] if isinstance(summary, str) else ""
            ),
        )
    return None


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------
@dataclass
class SubmissionResult:
    action: str  # "matched" | "created"
    group: Group
    prior_count: int  # how many submissions the group had *before* this one


async def submit_suggestion(
    *, user_id: str, user_name: str, text: str
) -> Optional[SubmissionResult]:
    """Categorize a suggestion with Gemini and, only on success, persist it.

    Returns ``None`` when Gemini cannot categorize the idea — in that case
    nothing is written, so the caller can ask the user to retry.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty suggestion")
    text = text[:_MAX_IDEA_CHARS]

    store = _store()
    groups = await asyncio.to_thread(store.load)

    cls = await _classify(text, groups)
    if cls is None:
        return None  # categorized-or-nothing: do not persist.

    sub = Submission(user_id=user_id, user_name=user_name, text=text, at=_now_iso())

    if cls.match_id:
        target = next((g for g in groups if g.id == cls.match_id), None)
        if target is not None:
            prior = target.size
            target.add(sub)
            await store.save(groups)
            result = SubmissionResult(action="matched", group=target, prior_count=prior)
            _track(result, user_id)
            return result

    group = Group(
        id=_new_group_id(),
        title=cls.new_title or text[:_MAX_TITLE_CHARS],
        summary=cls.new_summary or "",
        created_at=_now_iso(),
        updated_at=_now_iso(),
        submissions=[],
    )
    group.add(sub)
    groups.append(group)
    await store.save(groups)
    result = SubmissionResult(action="created", group=group, prior_count=0)
    _track(result, user_id)
    return result


def _track(result: SubmissionResult, user_id: str) -> None:
    """Register the categorized suggestion in PostHog (fire-and-forget)."""
    try:
        analytics.capture(
            "suggestion_submitted",
            distinct_id=user_id,
            properties={
                "action": result.action,
                "group_id": result.group.id,
                "group_title": result.group.title,
                "group_size": result.group.size,
            },
        )
    except Exception:
        logger.debug("suggestions: analytics capture failed", exc_info=True)


def _format_reply(result: SubmissionResult) -> str:
    g = result.group
    title = g.title or "(sin titulo)"
    if result.action == "matched":
        prior = result.prior_count
        plural = "es" if prior != 1 else ""
        estaban = "estaban" if prior != 1 else "estaba"
        return (
            f"✅ Sumé tu idea al grupo **{title}** "
            f"({prior} sugerencia{'s' if prior != 1 else ''} similar{plural} ya {estaban} ahí)."
        )
    body = f"✅ Anotada como idea nueva: **{title}**."
    if g.summary:
        body += f"\n> {g.summary}"
    return body


async def sugerenciasLogic(ctx, idea: str) -> None:
    """Slash command handler for ``/sugerencias``.

    Validates the idea, categorizes it with Gemini and persists it only on
    success, then replies with a short ephemeral confirmation telling the user
    which group their idea joined.
    """
    text = (idea or "").strip()
    if not text:
        await _send(ctx, "decime que sugerencia tenes, no la dejes vacia")
        return

    user = ctx.author
    user_id = str(getattr(user, "id", "0"))
    user_name = (
        getattr(user, "display_name", None) or getattr(user, "name", None) or "anon"
    )

    try:
        result = await submit_suggestion(
            user_id=user_id, user_name=user_name, text=text
        )
    except ValueError:
        await _send(ctx, "decime que sugerencia tenes, no la dejes vacia")
        return
    except Exception:
        logger.exception("sugerencias: submit failed")
        await _send(
            ctx, "se rompio algo guardando la sugerencia, probá de nuevo en un rato"
        )
        return

    if result is None:
        await _send(
            ctx,
            "no pude clasificar tu idea ahora mismo (el clasificador no está "
            "disponible). Probá de nuevo en un rato — no se guardó nada todavía.",
        )
        return

    await _send(ctx, _format_reply(result))


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------
def _format_listing(groups: list[Group]) -> str:
    ranked = sorted(groups, key=lambda g: (g.size, g.updated_at), reverse=True)
    lines = ["💡 **Sugerencias acumuladas** (ordenadas por más pedidas):", ""]
    for g in ranked[:_VER_MAX_GROUPS]:
        n = g.size
        line = f"• **{g.title}** ({n} pedido{'s' if n != 1 else ''})"
        if g.summary:
            line += f" — {g.summary}"
        lines.append(line)
    extra = len(ranked) - _VER_MAX_GROUPS
    if extra > 0:
        lines.append("")
        lines.append(f"…y {extra} grupo{'s' if extra != 1 else ''} más.")
    return "\n".join(lines)


async def sugerenciasVerLogic(ctx) -> None:
    """Slash command handler for ``/sugerencias-ver``.

    Lists the existing suggestion groups ranked by how many people asked for
    each, so users can see what ideas already exist.
    """
    try:
        groups = await asyncio.to_thread(_store().load)
    except Exception:
        logger.exception("sugerencias-ver: load failed")
        await _send(
            ctx, "no pude leer las sugerencias ahora, probá de nuevo en un rato"
        )
        return

    if not groups:
        await _send(
            ctx,
            "todavía no hay ninguna sugerencia. Mandá la primera con `/sugerencias`.",
        )
        return

    analytics.capture(
        "suggestions_viewed", distinct_id=str(getattr(ctx.author, "id", "0"))
    )
    await _send(ctx, _format_listing(groups))


async def _send(ctx, message: str) -> None:
    try:
        await ctx.followup.send(message, ephemeral=True)
    except Exception:
        logger.exception("sugerencias: reply send failed")
