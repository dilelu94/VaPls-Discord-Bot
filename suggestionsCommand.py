"""User suggestions bucket — `/sugerencias` slash command.

Recibe ideas de feature/cambios de los usuarios, las pasa por Gemini Flash-Lite
para agrupar ideas similares con las que ya existen, y persiste todo en un JSON
para revisar despues. El objetivo es que la idea general no se pierda, no
implementar nada automaticamente.

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
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
import geminiClient

logger = logging.getLogger("bot.suggestions")

_MAX_IDEA_CHARS = 1000
_MAX_TITLE_CHARS = 80
_MAX_SUMMARY_CHARS = 240
_PERSIST_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_group_id() -> str:
    return f"g_{uuid.uuid4().hex[:8]}"


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
        except OSError:
            pass
        raise


def _load_state() -> dict:
    path = config.SUGGESTIONS_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"groups": []}
    except Exception:
        logger.exception("suggestions load failed at %s", path)
        return {"groups": []}
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return {"groups": []}
    return {"groups": groups}


async def _save_state(state: dict) -> None:
    path = config.SUGGESTIONS_PATH
    async with _PERSIST_LOCK:
        try:
            await asyncio.to_thread(_write_json_atomic, path, state)
        except Exception:
            logger.exception("suggestions persist failed at %s", path)


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


def _classify_prompt(idea: str, groups: list[dict]) -> str:
    if groups:
        lines = ["Grupos existentes:"]
        for g in groups:
            lines.append(
                f"- id={g.get('id')} | titulo={g.get('title','')} | "
                f"resumen={g.get('summary','')}"
            )
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
        obj = json.loads(s[start:end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


@dataclass
class Classification:
    match_id: Optional[str] = None
    new_title: Optional[str] = None
    new_summary: Optional[str] = None


async def _classify(idea: str, groups: list[dict]) -> Optional[Classification]:
    """Run Gemini Flash-Lite. Returns ``None`` when classification fails so the
    caller can decide on the fallback (raw save without grouping)."""
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
        if any(g.get("id") == match_id.strip() for g in groups):
            return Classification(match_id=match_id.strip())
        logger.warning("suggestions: classifier returned unknown match_id %r", match_id)
    title = parsed.get("new_title")
    summary = parsed.get("new_summary")
    if isinstance(title, str) and title.strip():
        return Classification(
            new_title=title.strip()[:_MAX_TITLE_CHARS],
            new_summary=(summary.strip()[:_MAX_SUMMARY_CHARS]
                         if isinstance(summary, str) else ""),
        )
    return None


def _append_submission(group: dict, *, user_id: str, user_name: str, text: str) -> None:
    subs = group.setdefault("submissions", [])
    subs.append({
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "at": _now_iso(),
    })
    group["updated_at"] = _now_iso()


@dataclass
class SubmissionResult:
    action: str  # "matched" | "created" | "unprocessed"
    group: dict


async def submit_suggestion(*, user_id: str, user_name: str, text: str) -> SubmissionResult:
    """Persist a user suggestion, grouping it with similar ideas via Gemini.

    On classifier failure, the raw idea is still saved as its own
    "unprocessed" group so nothing is lost — the user can ask later for these
    to be reviewed and grouped manually.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty suggestion")
    text = text[:_MAX_IDEA_CHARS]

    state = await asyncio.to_thread(_load_state)
    groups: list[dict] = state["groups"]

    cls = await _classify(text, groups)

    if cls is None:
        # Fallback: save raw so the idea is never lost.
        group = {
            "id": _new_group_id(),
            "title": text[:_MAX_TITLE_CHARS],
            "summary": "(sin clasificar — Gemini no disponible)",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "submissions": [],
            "unprocessed": True,
        }
        _append_submission(group, user_id=user_id, user_name=user_name, text=text)
        groups.append(group)
        await _save_state(state)
        return SubmissionResult(action="unprocessed", group=group)

    if cls.match_id:
        target = next((g for g in groups if g.get("id") == cls.match_id), None)
        if target is not None:
            _append_submission(target, user_id=user_id, user_name=user_name, text=text)
            await _save_state(state)
            return SubmissionResult(action="matched", group=target)

    title = cls.new_title or text[:_MAX_TITLE_CHARS]
    summary = cls.new_summary or ""
    group = {
        "id": _new_group_id(),
        "title": title,
        "summary": summary,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "submissions": [],
    }
    _append_submission(group, user_id=user_id, user_name=user_name, text=text)
    groups.append(group)
    await _save_state(state)
    return SubmissionResult(action="created", group=group)


def _format_reply(result: SubmissionResult) -> str:
    g = result.group
    count = len(g.get("submissions") or [])
    title = g.get("title", "(sin titulo)")
    if result.action == "matched":
        return (
            f"✅ Ya teniamos una idea parecida: **{title}**. "
            f"La sume al grupo ({count} sugerencia{'s' if count != 1 else ''} ahora)."
        )
    if result.action == "created":
        summary = g.get("summary") or ""
        body = f"✅ Anotada como **{title}**."
        if summary:
            body += f"\n> {summary}"
        return body
    return (
        f"✅ Guardada como **{title}**. "
        f"No pude agrupar automaticamente, pero quedo registrada para revisar."
    )


async def sugerenciasLogic(ctx, idea: str) -> None:
    """Slash command handler for ``/sugerencias``.

    Validates the idea, persists it (grouping when Gemini is available), and
    replies to the user with a short ephemeral confirmation.
    """
    text = (idea or "").strip()
    if not text:
        try:
            await ctx.followup.send(
                "decime que sugerencia tenes, no la dejes vacia",
                ephemeral=True,
            )
        except Exception:
            logger.exception("sugerencias: empty-reply send failed")
        return

    user = ctx.author
    user_id = str(getattr(user, "id", "0"))
    user_name = (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or "anon"
    )

    try:
        result = await submit_suggestion(
            user_id=user_id, user_name=user_name, text=text,
        )
    except ValueError:
        try:
            await ctx.followup.send(
                "decime que sugerencia tenes, no la dejes vacia",
                ephemeral=True,
            )
        except Exception:
            logger.exception("sugerencias: empty-reply send failed")
        return
    except Exception:
        logger.exception("sugerencias: submit failed")
        try:
            await ctx.followup.send(
                "se rompio algo guardando la sugerencia, probá de nuevo en un rato",
                ephemeral=True,
            )
        except Exception:
            pass
        return

    try:
        await ctx.followup.send(_format_reply(result), ephemeral=True)
    except Exception:
        logger.exception("sugerencias: reply send failed")
