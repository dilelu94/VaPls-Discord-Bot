"""Automated Weekly Patch Notes Generator for VaPls & El Indio.

Analyzes the past 7 days of git commits and documentation changes using Gemini
to dynamically detect what actually changed (features, fixes, bot persona, music,
GoLive, etc.), generates a 24h expiring web page, and posts a concise update
to Discord every Friday at 19:00 hs (UTC-3).
"""

import asyncio
import datetime
import json
import logging
import os
import re
from typing import Any, Optional

import config
import geminiClient
from patch_notes import get_patch_notes_url, patch_notes_manager

logger = logging.getLogger("vapls.patch_notes_generator")

# Timezone for Argentina (UTC-3)
TZ_ARG = datetime.timezone(datetime.timedelta(hours=-3))


async def get_recent_git_commits(days: int = 7, repo_dir: Optional[str] = None) -> list[str]:
    """Retrieve non-merge git commit subjects from the last `days` days."""
    cmd = [
        "git",
        "log",
        f"--since={days} days ago",
        "--no-merges",
        "--pretty=format:%h - %s",
    ]
    cwd = repo_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("[PATCH NOTES] git log failed with code %d: %s", proc.returncode, stderr.decode().strip())
            return []
        lines = [line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        logger.info("[PATCH NOTES] Retrieved %d git commits from the last %d days.", len(lines), days)
        return lines
    except Exception as exc:
        logger.exception("[PATCH NOTES] Error executing git log: %s", exc)
        return []


def _extract_json_from_gemini_response(text: str) -> Optional[dict[str, Any]]:
    """Extract and parse JSON object from Gemini markdown response."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = match.group(1) if match else text
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except Exception:
        # Try finding outermost { } braces
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return None


def _build_fallback_patch_notes(commits: list[str], date_str: str) -> dict[str, Any]:
    """Build structured patch notes without Gemini when API is unavailable."""
    version = "2." + datetime.datetime.now(TZ_ARG).strftime("%m.%d")
    title = f"Notas de Parche ({date_str})"

    # Classify commits into groups
    items: list[dict[str, Any]] = []
    for c in commits[:10]:
        tag = "Fix" if "fix" in c.lower() else ("Buff" if "feat" in c.lower() else "Mejora")
        clean_msg = c
        if " - " in c:
            clean_msg = c.split(" - ", 1)[1]
        clean_msg = re.sub(r"^(?:feat|fix|docs|refactor|chore|test)\([^)]+\):\s*", "", clean_msg).capitalize()
        items.append({
            "tag": tag,
            "tag_type": "fix" if tag == "Fix" else ("buff" if tag == "Buff" else "qol"),
            "header": clean_msg,
            "desc": clean_msg,
        })

    discord_items = []
    for item in items[:2]:
        discord_items.append({
            "title": item["header"],
            "tag": item["tag"],
            "desc": item["desc"],
        })

    if not discord_items:
        discord_items = [
            {
                "title": "Mantenimiento General",
                "tag": "Mejora",
                "desc": "Ajustes de estabilidad y optimizaciones en el servidor.",
            }
        ]

    category_title = "🛠️ MEJORAS Y ARREGLOS DE LA SEMANA"
    if any("indio" in c.lower() or "stt" in c.lower() for c in commits):
        category_title = "🧠 RESPUESTAS Y COMPORTAMIENTO DEL INDIO"
    elif any("stream" in c.lower() or "golive" in c.lower() or "video" in c.lower() for c in commits):
        category_title = "📺 STREAMING & GOLIVE"
    elif any("play" in c.lower() or "audio" in c.lower() or "musica" in c.lower() for c in commits):
        category_title = "🎵 REPRODUCTOR DE MÚSICA"

    return {
        "version": version,
        "title": title,
        "discord_highlight": {
            "category_title": category_title,
            "items": discord_items,
        },
        "sections": [
            {
                "icon": "🛠️",
                "title": "Cambios Principales",
                "items": items if items else [
                    {
                        "tag": "Mejora",
                        "tag_type": "qol",
                        "header": "Mantenimiento General",
                        "desc": "Ajustes de estabilidad y optimizaciones en el servidor.",
                    }
                ],
            }
        ],
    }


async def generate_patch_notes_with_gemini(commits: list[str], date_str: str) -> dict[str, Any]:
    """Call Gemini to analyze weekly commits and generate patch notes structure."""
    if not commits:
        return _build_fallback_patch_notes(commits, date_str)

    commit_list_text = "\n".join(f"- {c}" for c in commits)

    system_instruction = (
        "Sos el redactor oficial de las notas de parche semanales del bot de Discord VaPls y El Indio. "
        "Tu tarea es analizar los commits de git de los últimos 7 días y resumir los cambios de forma ingeniosa, "
        "canchera, con humor y fiel al estilo del grupo de amigos de Argentina (VaPls / El Indio).\n\n"
        "REGLAS OBLIGATORIAS:\n"
        "1. NO uses palabras cringe ni de 'gamer exagerado' (PROHIBIDO hablar de 'combate', 'hitscan', 'nerfs tácticos' "
        "o cosas autistas a menos que aplique con humor exacto a una función real).\n"
        "2. Identificá qué fue lo que REALMENTE cambió según los commits de la semana: si fue música, streaming GoLive, "
        "el Indio, comandos, correcciones de errores, base de datos, etc. La categoría destacada en Discord DEBE basarse "
        "en los cambios reales de los commits, no inventes cosas que no cambiaron.\n"
        "3. El JSON devuelto debe tener un 'discord_highlight' con:\n"
        "   - 'category_title': Un emoji seguido de un título en mayúsculas (ej: '🧠 RESPUESTAS Y COMPORTAMIENTO DEL INDIO', "
        "'📺 STREAMING & GOLIVE', '🎵 REPRODUCTOR DE MÚSICA', '🛠️ ARREGLOS Y ESTABILIDAD').\n"
        "   - 'items': Lista de exactamente 1 o 2 cambios destacados concisos.\n"
        "     Cada uno con 'title' (título corto), 'tag' ('Buff', 'Fix', 'Nuevo', 'Mejora' o 'Ajuste'), y 'desc' (explicación concisa y divertida).\n"
        "4. El JSON devuelto debe tener 'sections' para la página web completa con las secciones agrupadas por área "
        "(cada sección con 'icon', 'title', y lista de 'items' con 'tag', 'tag_type' (buff/fix/nerf/feat/qol), 'header', 'desc', y opcional 'dialogues').\n"
        "5. Devolvé ÚNICAMENTE un bloque JSON válido."
    )

    user_prompt = (
        f"Fecha del parche: {date_str}\n"
        f"Commits de la semana:\n{commit_list_text}\n\n"
        "Generá el JSON con la estructura:\n"
        "{\n"
        '  "version": "2.7",\n'
        f'  "title": "Notas de Parche ({date_str})",\n'
        '  "discord_highlight": {\n'
        '    "category_title": "<EMOJI> <CATEGORÍA DESTACADA>",\n'
        '    "items": [\n'
        '      {"title": "<Nombre>", "tag": "<Buff/Fix/Nuevo/Mejora>", "desc": "<Descripción concisa>"}\n'
        "    ]\n"
        "  },\n"
        '  "sections": [\n'
        "    {\n"
        '      "icon": "<Emoji>",\n'
        '      "title": "<Título Sección>",\n'
        '      "items": [\n'
        '        {"tag": "<Buff/Fix...>", "tag_type": "buff|fix|nerf|feat|qol", "header": "<Título>", "desc": "<Detalle>", "dialogues": ["..."]}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )

    try:
        logger.info("[PATCH NOTES] Sending %d commit(s) to Gemini API for dynamic patch notes analysis...", len(commits))
        reply = await geminiClient.generate(
            user_message=user_prompt,
            system_instruction=system_instruction,
            timeout_sec=40,
        )
        parsed = _extract_json_from_gemini_response(reply.text)
        if parsed and "discord_highlight" in parsed and "sections" in parsed:
            cat = parsed["discord_highlight"].get("category_title", "Unknown")
            h_items = len(parsed["discord_highlight"].get("items", []))
            secs = len(parsed.get("sections", []))
            logger.info(
                "[PATCH NOTES] Gemini successfully generated patch notes (category='%s', highlight_items=%d, sections=%d)",
                cat,
                h_items,
                secs,
            )
            return parsed
        logger.warning(
            "[PATCH NOTES] Gemini response did not contain expected JSON structure: %s. Using fallback.",
            reply.text[:200],
        )
    except Exception as exc:
        logger.warning(
            "[PATCH NOTES] Gemini API call failed (%s: %s). Activating automatic structured fallback.",
            type(exc).__name__,
            exc,
        )

    return _build_fallback_patch_notes(commits, date_str)


def format_discord_patch_notes_message(date_str: str, highlight_data: dict[str, Any], url: str) -> str:
    """Format the Discord announcement strictly adhering to user guidelines."""
    category_title = highlight_data.get("category_title", "🛠️ MEJORAS Y ARREGLOS DE LA SEMANA").strip()
    items = highlight_data.get("items", [])

    bullets = []
    for item in items:
        title = item.get("title", "").strip()
        tag = item.get("tag", "Mejora").strip()
        desc = item.get("desc", "").strip()
        bullets.append(f"• **{title} ({tag}):** {desc}")

    bullets_text = "\n".join(bullets)
    if not bullets_text:
        bullets_text = "• **Mantenimiento General (Mejora):** Optimizaciones de estabilidad en el sistema."

    return (
        f"**Notas del parche vapls ({date_str})**\n\n"
        f"{category_title}\n\n"
        f"{bullets_text}\n\n"
        f"🔗 [Ver notas del parche completo]({url})"
    )


def load_cron_state(path: Optional[str] = None) -> dict[str, Any]:
    """Load persistent state for weekly patch notes cron."""
    state_file = path or getattr(config, "PATCH_NOTES_CRON_STATE_PATH", "data/patch_notes_cron_state.json")
    if not os.path.isfile(state_file):
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not read patch notes cron state: %s", exc)
        return {}


def save_cron_state(state: dict[str, Any], path: Optional[str] = None) -> None:
    """Save persistent state for weekly patch notes cron."""
    state_file = path or getattr(config, "PATCH_NOTES_CRON_STATE_PATH", "data/patch_notes_cron_state.json")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(state_file)), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        logger.exception("[PATCH NOTES] Could not save patch notes cron state: %s", exc)


async def generate_and_post_weekly_patch_notes(
    bot: Any,
    force: bool = False,
    channel_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Generate weekly patch notes and post to Discord #soreteposting."""
    now = datetime.datetime.now(TZ_ARG)
    today_iso = now.strftime("%Y-%m-%d")
    date_formatted = now.strftime("%d/%m/%Y")

    if not force:
        # Check if today is Friday (weekday 4 in Python: Monday=0, Sunday=6)
        if now.weekday() != 4:
            logger.info("[PATCH NOTES] Today (%s) is not Friday (weekday=%d). Skipping weekly patch notes.", today_iso, now.weekday())
            return None

        # Check if already executed today
        state = load_cron_state()
        if state.get("last_run_date") == today_iso:
            logger.info("[PATCH NOTES] Weekly patch notes already executed today (%s). Skipping.", today_iso)
            return None

    logger.info("[PATCH NOTES] Generating weekly patch notes (date=%s, force=%s)...", date_formatted, force)

    commits = await get_recent_git_commits(days=7)
    if not commits and not force:
        logger.info("[PATCH NOTES] No commits found in the last 7 days. Skipping weekly patch notes.")
        return None

    patch_data = await generate_patch_notes_with_gemini(commits, date_formatted)
    version = str(patch_data.get("version", "2.7"))
    title = str(patch_data.get("title", f"Notas de Parche ({date_formatted})"))
    sections = patch_data.get("sections", [])
    highlight = patch_data.get("discord_highlight", {})

    # Create 24h expiring token with dynamic sections and archive in history
    token = patch_notes_manager.create_token(
        version=version,
        ttl_seconds=86400,
        title=title,
        sections=sections,
        discord_highlight=highlight,
        save_history=True,
    )
    url = get_patch_notes_url(token)
    logger.info("[PATCH NOTES] Created token %s (ttl=24h). URL: %s", token[:8], url)
    message_text = format_discord_patch_notes_message(date_formatted, highlight, url)

    # Resolve target Discord channel
    target_channel = None
    if channel_id and hasattr(bot, "get_channel"):
        target_channel = bot.get_channel(channel_id)

    if not target_channel:
        try:
            import geminiCommand

            target_channel = geminiCommand._get_soreteposting_channel(bot)
            if target_channel:
                logger.info("[PATCH NOTES] Resolved #soreteposting channel via geminiCommand (id=%s)", getattr(target_channel, "id", None))
        except Exception as exc:
            logger.debug("[PATCH NOTES] Failed getting soreteposting channel via geminiCommand: %s", exc)

    if not target_channel and hasattr(bot, "get_channel"):
        story_ch_id = getattr(config, "INDIO_STORY_CHANNEL_ID", 451580655650996236)
        if story_ch_id:
            target_channel = bot.get_channel(story_ch_id)
            if target_channel:
                logger.info("[PATCH NOTES] Resolved fallback story channel (id=%s)", story_ch_id)

    sent_msg = None
    if target_channel and hasattr(target_channel, "send"):
        try:
            sent_msg = await target_channel.send(message_text)
            logger.info(
                "[PATCH NOTES] Successfully posted weekly patch notes to #%s (channel_id=%s, msg_id=%s)",
                getattr(target_channel, "name", "channel"),
                getattr(target_channel, "id", None),
                getattr(sent_msg, "id", None),
            )
        except Exception as exc:
            logger.exception("[PATCH NOTES] Failed to send weekly patch notes to Discord channel: %s", exc)
            raise
    else:
        err = RuntimeError("No valid Discord text channel found (#soreteposting) to post weekly patch notes.")
        logger.exception("[PATCH NOTES] %s", err)
        raise err

    # Persist state
    state = load_cron_state()
    state["last_run_date"] = today_iso
    save_cron_state(state)
    logger.info("[PATCH NOTES] Successfully persisted cron run state (last_run_date=%s).", today_iso)

    return {
        "token": token,
        "url": url,
        "message_text": message_text,
        "message_id": getattr(sent_msg, "id", None) if sent_msg else None,
        "channel_id": getattr(target_channel, "id", None) if target_channel else None,
    }
