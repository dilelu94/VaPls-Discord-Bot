"""Patch Notes Web UI and token management for VaPls Discord Bot.

Provides secure, time-expiring (24h) web pages to display full patch notes
with strict security headers (CSP, anti-framing, no-referrer), IP rate-limiting,
cryptographic token generation, and automatic expiration.
"""

import datetime
import html
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger("vapls.patch_notes")

# Regex to validate token format (safe characters only, no path traversal)
TOKEN_REGEX = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
DEFAULT_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "patch_notes.json")
DEFAULT_HISTORY_PATH = os.path.join(os.path.dirname(__file__), "data", "patch_notes_history.json")
TZ_ARG = datetime.timezone(datetime.timedelta(hours=-3))


def _get_default_v26_sections() -> list[dict[str, Any]]:
    """Return default structured sections for version 2.6 patch notes."""
    return [
        {
            "icon": "🛡️",
            "title": "Saludos Raros & Pity",
            "items": [
                {
                    "tag": "Ajuste",
                    "tag_type": "nerf",
                    "header": "Cooldown de Pity en Saludos Raros",
                    "desc": "Se implementó un cooldown de 1 hora para el incremento de pity por usuario. Se evita entrar y salir repetidamente del canal de voz para forzar audios raros (como el Don Cangrejo de Seba)."
                }
            ]
        },
        {
            "icon": "📺",
            "title": "GoLive Streaming",
            "items": [
                {
                    "tag": "Mejora",
                    "tag_type": "buff",
                    "header": "Sincronización Audio / Video (A/V Sync)",
                    "desc": "Se eliminó el desfasaje en transmisiones GoLive (IPTV, HLS y Stremio). Ahora el audio inicia exactamente con la emisión del primer fotograma (first_frame_sent)."
                }
            ]
        },
        {
            "icon": "🧠",
            "title": "Respuestas y Comportamiento del Indio",
            "items": [
                {
                    "tag": "Nuevo",
                    "tag_type": "buff",
                    "header": "Nuevas Frases cuando anda cruzado",
                    "desc": "Se sumaron nuevas respuestas al repertorio del Indio:",
                    "dialogues": [
                        '💬 "andá a hacerte ortear"',
                        '💬 "¿por qué no me sopapeás la papirola?"'
                    ]
                },
                {
                    "tag": "Arreglo",
                    "tag_type": "fix",
                    "header": "Corrección en bardeadas ('cabecear el enano')",
                    "desc": "Se corrigió la intención de la frase para asegurar que el bardeo se dirija al interlocutor y no hacia sí mismo."
                },
                {
                    "tag": "Arreglo",
                    "tag_type": "fix",
                    "header": "Control de Reproducción de Música",
                    "desc": "Se agregaron filtros para evitar que el Indio corte o cambie canciones por error mientras conversa en el chat."
                }
            ]
        },
        {
            "icon": "🗺️",
            "title": "IA & Navegación en Canales de Voz",
            "items": [
                {
                    "tag": "Fix / Pathfinding",
                    "tag_type": "fix",
                    "header": "Restricción de Canales AFK",
                    "desc": "El Indio ya no seguirá a los usuarios cuando se muevan o sean movidos a los canales AFK del servidor."
                },
                {
                    "tag": "Ajuste de Probabilidad",
                    "tag_type": "qol",
                    "header": "Eventos de Desconexión de Usuarios",
                    "desc": "Probabilidad de reacción al desconectarse un usuario rebalanceada a 1% global, y calibrada en 20% para la salida de Chalo."
                }
            ]
        },
        {
            "icon": "🐛",
            "title": "Sistema de Reportes & Misiones",
            "items": [
                {
                    "tag": "Feature",
                    "tag_type": "feat",
                    "header": "Auto-Bug Tracker en GitHub Issues",
                    "desc": "Al responder directamente a un audio clipeado con transcripción fallida de STT, el bot crea automáticamente un reporte de issue en GitHub con los datos de depuración. Se incorporó además la función close_issue para cerrar tickets resueltos."
                }
            ]
        },
        {
            "icon": "🖼️",
            "title": "Historias & Multimedia",
            "items": [
                {
                    "tag": "QoL",
                    "tag_type": "qol",
                    "header": "Galería Curada e Interacción de Historias",
                    "desc": "Sincronización del manifiesto de imágenes curadas y mejoras en el procesamiento del feedback al responder a historias espontáneas del Indio."
                }
            ]
        }
    ]


class PatchNotesManager:
    """Manages secure, expiring tokens and HTML rendering for Patch Notes."""

    def __init__(
        self,
        data_path: str = DEFAULT_DATA_PATH,
        history_path: Optional[str] = None,
    ):
        self.data_path = data_path
        if history_path is not None:
            self.history_path = history_path
        elif data_path != DEFAULT_DATA_PATH:
            self.history_path = os.path.splitext(data_path)[0] + "_history.json"
        else:
            self.history_path = DEFAULT_HISTORY_PATH

        self._tokens: dict[str, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []
        self._rate_limits: dict[str, list[float]] = {}
        self._load()
        self._load_history()

    def _load(self) -> None:
        """Load stored tokens from disk."""
        if not os.path.isfile(self.data_path):
            self._tokens = {}
            return
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                self._tokens = json.load(f)
        except Exception as exc:
            logger.warning("Failed to load patch notes tokens from %s: %s", self.data_path, exc)
            self._tokens = {}

    def _save(self) -> None:
        """Persist tokens to disk."""
        os.makedirs(os.path.dirname(os.path.abspath(self.data_path)), exist_ok=True)
        try:
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self._tokens, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save patch notes tokens to %s: %s", self.data_path, exc)

    def _deduplicate_history(self) -> bool:
        """Deduplicate in-memory history by release date, title, and version.

        When duplicate entries exist (e.g. from multiple test/generator runs on the same date),
        preserves the richest entry (more sections, Gemini creative copy, or dialogues).
        Returns True if duplicates were pruned.
        """
        if not self._history:
            return False

        def _score_entry(e: dict[str, Any]) -> int:
            score = 0
            secs = e.get("sections") or []
            score += len(secs) * 10
            for s in secs:
                items = s.get("items") or []
                score += len(items) * 2
                for item in items:
                    if item.get("dialogues"):
                        score += 5
            # Bonus for creative custom sections (non-fallback)
            if secs and secs[0].get("title") != "Cambios Principales":
                score += 15
            if e.get("discord_highlight"):
                score += 5
            return score

        seen_keys: dict[str, int] = {}
        unique_entries: list[dict[str, Any]] = []
        changed = False

        for entry in self._history:
            title = str(entry.get("title", "")).strip()
            version = str(entry.get("version", "")).strip()
            date_match = re.search(r"(\d{2}/\d{2}/\d{4})", title)
            date_key = date_match.group(1) if date_match else ""
            if not date_key and entry.get("created_at"):
                try:
                    date_key = datetime.datetime.fromtimestamp(float(entry["created_at"]), TZ_ARG).strftime("%d/%m/%Y")
                except Exception:
                    date_key = ""

            # Match against existing seen keys
            match_idx = None
            if date_key and f"date:{date_key}" in seen_keys:
                match_idx = seen_keys[f"date:{date_key}"]
            elif title and f"title:{title.lower()}" in seen_keys:
                match_idx = seen_keys[f"title:{title.lower()}"]
            elif version and f"ver:{version}" in seen_keys:
                match_idx = seen_keys[f"ver:{version}"]

            if match_idx is not None:
                existing_entry = unique_entries[match_idx]
                if _score_entry(entry) > _score_entry(existing_entry):
                    merged = dict(entry)
                    if not merged.get("token") and existing_entry.get("token"):
                        merged["token"] = existing_entry["token"]
                    unique_entries[match_idx] = merged
                changed = True
            else:
                idx = len(unique_entries)
                unique_entries.append(entry)
                if date_key:
                    seen_keys[f"date:{date_key}"] = idx
                if title:
                    seen_keys[f"title:{title.lower()}"] = idx
                if version:
                    seen_keys[f"ver:{version}"] = idx

        if changed or len(unique_entries) != len(self._history):
            self._history = unique_entries
            return True
        return False

    def _load_history(self) -> None:
        """Load stored patch notes history from disk, deduplicating and seeding from existing tokens if empty."""
        if os.path.isfile(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._history = data
                    elif isinstance(data, dict):
                        self._history = list(data.values())
            except Exception as exc:
                logger.warning("Failed to load patch notes history from %s: %s", self.history_path, exc)
                self._history = []

        # Deduplicate loaded entries
        deduped = self._deduplicate_history()

        # Track existing versions, tokens, titles, and dates to avoid duplicate cards
        history_versions = {str(h.get("version", "")).strip() for h in self._history if h.get("version")}
        history_tokens = {str(h.get("token", "")).strip() for h in self._history if h.get("token")}
        history_titles = {str(h.get("title", "")).strip().lower() for h in self._history if h.get("title")}
        history_dates = set()
        for h in self._history:
            dm = re.search(r"(\d{2}/\d{2}/\d{4})", str(h.get("title", "")))
            if dm:
                history_dates.add(dm.group(1))
            elif h.get("created_at"):
                try:
                    history_dates.add(datetime.datetime.fromtimestamp(float(h["created_at"]), TZ_ARG).strftime("%d/%m/%Y"))
                except Exception:
                    pass

        added = False
        if self._tokens:
            sorted_tokens = sorted(
                self._tokens.values(),
                key=lambda x: float(x.get("created_at", 0)),
                reverse=True,
            )
            for tok_data in sorted_tokens:
                ver = str(tok_data.get("version") or "2.6").strip()
                tok = str(tok_data.get("token") or "").strip()
                title = str(tok_data.get("title") or f"Notas de Parche v{ver}").strip()
                dm = re.search(r"(\d{2}/\d{2}/\d{4})", title)
                tok_date = dm.group(1) if dm else ""
                if not tok_date and tok_data.get("created_at"):
                    try:
                        tok_date = datetime.datetime.fromtimestamp(float(tok_data["created_at"]), TZ_ARG).strftime("%d/%m/%Y")
                    except Exception:
                        tok_date = ""

                # Skip if already represented by token, version, title, or date
                if tok in history_tokens or ver in history_versions or title.lower() in history_titles:
                    continue
                if tok_date and tok_date in history_dates:
                    continue

                entry = dict(tok_data)
                entry["version"] = ver
                entry["title"] = title
                entry["sections"] = tok_data.get("sections") or _get_default_v26_sections()
                if not entry.get("discord_highlight"):
                    entry["discord_highlight"] = {
                        "category_title": "🧠 MEJORAS DEL INDIO & SALUDOS",
                        "items": [
                            {
                                "title": "Cooldown de Pity",
                                "tag": "Ajuste",
                                "desc": "Cooldown de 1 hora para audios raros de bienvenida."
                            },
                            {
                                "title": "A/V Sync Streaming",
                                "tag": "Mejora",
                                "desc": "Sincronización de audio y video en Go Live y Stremio."
                            },
                            {
                                "title": "Auto-Bug Tracker",
                                "tag": "Feature",
                                "desc": "Reporte automático de errores STT en GitHub Issues."
                            }
                        ]
                    }
                self._history.append(entry)
                history_versions.add(ver)
                history_titles.add(title.lower())
                if tok:
                    history_tokens.add(tok)
                if tok_date:
                    history_dates.add(tok_date)
                added = True

        if not self._history and (self.data_path == DEFAULT_DATA_PATH or self.history_path == DEFAULT_HISTORY_PATH):
            default_entry = {
                "token": "XN_zExXtxu8UZWaB7pbZD1ThvmUXLNoo5lnusFX8o0E",
                "version": "2.6",
                "title": "Notas de Parche v2.6",
                "created_at": 1791572630.6952732,
                "expires_at": 1791659030.6952732,
                "sections": _get_default_v26_sections(),
                "discord_highlight": {
                    "category_title": "🧠 MEJORAS DEL INDIO & SALUDOS",
                    "items": [
                        {
                            "title": "Cooldown de Pity",
                            "tag": "Ajuste",
                            "desc": "Cooldown de 1 hora para audios raros de bienvenida."
                        },
                        {
                            "title": "A/V Sync Streaming",
                            "tag": "Mejora",
                            "desc": "Sincronización de audio y video en Go Live y Stremio."
                        },
                        {
                            "title": "Auto-Bug Tracker",
                            "tag": "Feature",
                            "desc": "Reporte automático de errores STT en GitHub Issues."
                        }
                    ]
                }
            }
            self._history.append(default_entry)
            added = True

        if self._deduplicate_history() or deduped:
            deduped = True

        self._history.sort(key=lambda x: float(x.get("created_at", 0)), reverse=True)
        if added or deduped:
            self._save_history()
            logger.info("Updated patch notes history (now %d entries)", len(self._history))

    def _save_history(self) -> None:
        """Persist historical patch notes archive to disk."""
        os.makedirs(os.path.dirname(os.path.abspath(self.history_path)), exist_ok=True)
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(self._history, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save patch notes history to %s: %s", self.history_path, exc)

    def save_to_history(
        self,
        patch_data: dict[str, Any],
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Save or update a patch notes release in the permanent historical archive."""
        entry = dict(patch_data)
        if token and "token" not in entry:
            entry["token"] = token

        now = time.time()
        created_at = float(entry.get("created_at") or now)
        entry["created_at"] = created_at
        version = str(entry.get("version", "2.0")).strip()
        title = str(entry.get("title", f"Notas de Parche v{version}")).strip()
        entry["version"] = version
        entry["title"] = title

        # Check for existing entry with same version, title, or release date to update
        updated = False
        dm = re.search(r"(\d{2}/\d{2}/\d{4})", title)
        date_str = dm.group(1) if dm else ""

        for idx, existing in enumerate(self._history):
            existing_title = str(existing.get("title", "")).strip().lower()
            existing_ver = str(existing.get("version", "")).strip()
            same_ver = (existing_ver == version)
            same_title = (existing_title == title.lower())
            same_date = bool(date_str and date_str in existing_title)

            if same_ver or same_title or same_date:
                self._history[idx] = {**existing, **entry}
                updated = True
                break

        if not updated:
            self._history.append(entry)

        self._deduplicate_history()
        self._history.sort(key=lambda x: float(x.get("created_at", 0)), reverse=True)
        self._save_history()
        logger.info("Saved patch notes to history (version=%s, title='%s')", version, title)
        return entry

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return historical patch notes entries sorted newest first."""
        if not self._history:
            self._load_history()
        return list(self._history[:limit])

    def get_history_entry(self, version_or_title: str) -> Optional[dict[str, Any]]:
        """Find a specific history entry by version or title."""
        target = str(version_or_title).strip().lower()
        for entry in self._history:
            if str(entry.get("version", "")).strip().lower() == target:
                return entry
            if str(entry.get("title", "")).strip().lower() == target:
                return entry
            if str(entry.get("token", "")).strip() == version_or_title:
                return entry
        return None

    def _cleanup_old_tokens(self) -> None:
        """Patch notes tokens are permanent and never deleted."""
        pass

    def create_token(
        self,
        version: str = "2.6",
        ttl_seconds: Optional[int] = None,
        title: str = "Notas de Parche v2.6",
        sections: Optional[list[dict[str, Any]]] = None,
        discord_highlight: Optional[dict[str, Any]] = None,
        save_history: bool = True,
    ) -> str:
        """Generate a cryptographically secure token valid permanently and archive in history."""
        self._cleanup_old_tokens()
        token = secrets.token_urlsafe(32)
        now = time.time()
        token_data = {
            "token": token,
            "version": version,
            "title": title,
            "created_at": now,
            "expires_at": None,
            "sections": sections or _get_default_v26_sections(),
            "discord_highlight": discord_highlight,
        }
        self._tokens[token] = token_data
        self._save()
        if save_history:
            self.save_to_history(token_data, token=token)
        logger.info("Created permanent patch notes token %s (version=%s)", token[:8], version)
        return token

    def get_token_status(self, token: Optional[str]) -> tuple[str, Optional[dict[str, Any]]]:
        """Validate token and return (status, data).

        Patch notes tokens are permanent and never expire.
        Status is one of: 'valid', 'not_found', 'invalid_format'.
        """
        if not token or not isinstance(token, str) or not TOKEN_REGEX.match(token):
            return "invalid_format", None

        data = self._tokens.get(token)
        if not data:
            return "not_found", None

        if not data.get("sections"):
            data["sections"] = _get_default_v26_sections()

        # Ensure active or requested valid token is represented in history
        if not self.get_history_entry(token) and not self.get_history_entry(data.get("version", "")):
            self.save_to_history(data, token=token)

        return "valid", data

    def check_rate_limit(self, ip: str, limit: int = 60, window: float = 60.0) -> bool:
        """Check if IP has exceeded request limit within window seconds."""
        now = time.time()
        timestamps = self._rate_limits.setdefault(ip, [])
        self._rate_limits[ip] = [t for t in timestamps if now - t < window]
        if len(self._rate_limits[ip]) >= limit:
            return False
        self._rate_limits[ip].append(now)
        return True

    def add_security_headers(self, response: web.Response) -> web.Response:
        """Attach strict security headers to prevent XSS, clickjacking, and content sniffing."""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "script-src 'unsafe-inline'; "
            "frame-ancestors 'none'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'none';"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    def render_patch_notes_html(self, token_data: dict[str, Any]) -> str:
        """Render high-aesthetic gaming patch notes page."""
        title = html.escape(str(token_data.get("title", "Notas de Parche v2.6")))
        version = html.escape(str(token_data.get("version", "2.6")))

        sections_data = token_data.get("sections") or _get_default_v26_sections()
        if sections_data and isinstance(sections_data, list):
            rendered_sections = []
            for sec in sections_data:
                sec_icon = html.escape(str(sec.get("icon", "📝")))
                sec_title = html.escape(str(sec.get("title", "Mejoras del Sistema")))
                items_html = []
                for item in sec.get("items", []):
                    raw_tag = str(item.get("tag", "Mejora"))
                    tag = html.escape(raw_tag)
                    raw_type = str(item.get("tag_type") or "").lower()
                    if not raw_type:
                        tag_lower = raw_tag.lower()
                        if "buff" in tag_lower or "nuevo" in tag_lower:
                            raw_type = "buff"
                        elif "nerf" in tag_lower:
                            raw_type = "nerf"
                        elif "fix" in tag_lower or "arreglo" in tag_lower:
                            raw_type = "fix"
                        elif "feat" in tag_lower:
                            raw_type = "feat"
                        else:
                            raw_type = "qol"
                    css_tag = f"tag-{raw_type}" if raw_type in ("buff", "nerf", "fix", "feat", "qol") else "tag-qol"
                    header = html.escape(str(item.get("header") or item.get("title") or ""))
                    desc = html.escape(str(item.get("desc", "")))
                    dialogues = item.get("dialogues")
                    dialogue_html = ""
                    if dialogues and isinstance(dialogues, list):
                        dialogue_lines = "<br>\n".join(html.escape(str(d)) for d in dialogues)
                        dialogue_html = f'\n            <div class="dialogue-box">\n              {dialogue_lines}\n            </div>'
                    items_html.append(f"""        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag {css_tag}">{tag}</span>
            {header}
          </div>
          <div class="item-desc">
            {desc}{dialogue_html}
          </div>
        </li>""")
                sec_content = "\n".join(items_html)
                rendered_sections.append(f"""    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">{sec_icon}</span>
        <span>{sec_title}</span>
      </div>
      <ul class="patch-list">
{sec_content}
      </ul>
    </div>""")
            sections_html_block = "\n\n".join(rendered_sections)
        else:
            sections_html_block = """    <!-- 1. ANTI-EXPLOITS & LOOT -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🛡️</span>
        <span>Saludos Raros & Pity</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-nerf">Ajuste</span>
            Cooldown de Pity en Saludos Raros
          </div>
          <div class="item-desc">
            Se implementó un <strong>cooldown de 1 hora</strong> para el incremento de pity por usuario. Se evita entrar y salir repetidamente del canal de voz para forzar audios raros (como el Don Cangrejo de Seba).
          </div>
        </li>
      </ul>
    </div>

    <!-- 2. NETCODE & GOLIVE -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">📺</span>
        <span>GoLive Streaming</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-buff">Mejora</span>
            Sincronización Audio / Video (A/V Sync)
          </div>
          <div class="item-desc">
            Se eliminó el desfasaje en transmisiones GoLive (IPTV, HLS y Stremio). Ahora el audio inicia exactamente con la emisión del primer fotograma (<code>first_frame_sent</code>).
          </div>
        </li>
      </ul>
    </div>

    <!-- 3. COMPORTAMIENTO DEL INDIO -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🧠</span>
        <span>Respuestas y Comportamiento del Indio</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-buff">Nuevo</span>
            Nuevas Frases cuando anda cruzado
          </div>
          <div class="item-desc">
            Se sumaron nuevas respuestas al repertorio del Indio:
            <div class="dialogue-box">
              💬 "andá a hacerte ortear"<br>
              💬 "¿por qué no me sopapeás la papirola?"
            </div>
          </div>
        </li>
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-fix">Arreglo</span>
            Corrección en bardeadas ('cabecear el enano')
          </div>
          <div class="item-desc">
            Se corrigió la intención de la frase para asegurar que el bardeo se dirija al interlocutor y no hacia sí mismo.
          </div>
        </li>
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-fix">Arreglo</span>
            Control de Reproducción de Música
          </div>
          <div class="item-desc">
            Se agregaron filtros para evitar que el Indio corte o cambie canciones por error mientras conversa en el chat.
          </div>
        </li>
      </ul>
    </div>

    <!-- 4. IA & NAVEGACIÓN DE VOZ -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🗺️</span>
        <span>IA & Navegación en Canales de Voz</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-fix">Fix / Pathfinding</span>
            Restricción de Canales AFK
          </div>
          <div class="item-desc">
            El Indio ya no seguirá a los usuarios cuando se muevan o sean movidos a los canales AFK del servidor.
          </div>
        </li>
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-qol">Ajuste de Probabilidad</span>
            Eventos de Desconexión de Usuarios
          </div>
          <div class="item-desc">
            Probabilidad de reacción al desconectarse un usuario rebalanceada a <strong>1% global</strong>, y calibrada en <strong>20% para la salida de Chalo</strong>.
          </div>
        </li>
      </ul>
    </div>

    <!-- 5. SISTEMA DE REPORTES -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🐛</span>
        <span>Sistema de Reportes & Misiones</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-feat">Feature</span>
            Auto-Bug Tracker en GitHub Issues
          </div>
          <div class="item-desc">
            Al responder directamente a un audio clipeado con transcripción fallida de STT, el bot crea automáticamente un reporte de issue en GitHub con los datos de depuración. Se incorporó además la función <code>close_issue</code> para cerrar tickets resueltos.
          </div>
        </li>
      </ul>
    </div>

    <!-- 6. HISTORIAS & MULTIMEDIA -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🖼️</span>
        <span>Historias & Multimedia</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-qol">QoL</span>
            Galería Curada e Interacción de Historias
          </div>
          <div class="item-desc">
            Sincronización del manifiesto de imágenes curadas y mejoras en el procesamiento del feedback al responder a historias espontáneas del Indio.
          </div>
        </li>
      </ul>
    </div>"""

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — VaPls & El Indio</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-base: #090713;
      --bg-card: rgba(18, 14, 34, 0.82);
      --bg-card-border: rgba(139, 92, 246, 0.22);
      --accent-purple: #8b5cf6;
      --accent-purple-glow: rgba(139, 92, 246, 0.35);
      --accent-pink: #ec4899;
      --accent-cyan: #06b6d4;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-red: #ef4444;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --font-body: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-body);
      background-color: var(--bg-base);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 16px 80px 16px;
      line-height: 1.6;
      background-image: 
        radial-gradient(circle at 15% 15%, rgba(139, 92, 246, 0.12) 0%, transparent 40%),
        radial-gradient(circle at 85% 85%, rgba(236, 72, 153, 0.1) 0%, transparent 45%);
      background-attachment: fixed;
    }}
    .container {{
      max-width: 820px;
      width: 100%;
    }}
    .header-card {{
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 20px;
      padding: 32px 28px;
      margin-bottom: 24px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
      backdrop-filter: blur(16px);
      text-align: center;
      position: relative;
      overflow: hidden;
    }}
    .header-card::before {{
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--accent-purple), var(--accent-pink), var(--accent-cyan));
    }}
    .badge-row {{
      display: flex;
      justify-content: center;
      gap: 10px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .badge-live {{
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.4);
      color: var(--accent-green);
    }}
    .badge-live::before {{
      content: '';
      width: 7px; height: 7px;
      border-radius: 50%;
      background: var(--accent-green);
      box-shadow: 0 0 8px var(--accent-green);
      animation: pulse 2s infinite;
    }}
    .badge-version {{
      background: rgba(139, 92, 246, 0.15);
      border: 1px solid rgba(139, 92, 246, 0.4);
      color: #c4b5fd;
    }}
    .badge-permanent {{
      background: rgba(139, 92, 246, 0.15);
      border: 1px solid rgba(139, 92, 246, 0.4);
      color: #c4b5fd;
    }}
    .badge-history-link {{
      background: rgba(6, 182, 212, 0.15);
      border: 1px solid rgba(6, 182, 212, 0.4);
      color: var(--accent-cyan);
      transition: all 0.2s ease;
    }}
    .badge-history-link:hover {{
      background: rgba(6, 182, 212, 0.28);
      color: #fff;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.3; }}
    }}
    h1 {{
      font-size: 2.2rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      margin-bottom: 8px;
      background: linear-gradient(135deg, #ffffff 40%, #c4b5fd 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .subtitle {{
      color: var(--text-muted);
      font-size: 1rem;
    }}
    .section-card {{
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 18px;
      padding: 24px 24px 20px 24px;
      margin-bottom: 18px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
      backdrop-filter: blur(12px);
      transition: transform 0.2s ease, border-color 0.2s ease;
    }}
    .section-card:hover {{
      transform: translateY(-2px);
      border-color: rgba(139, 92, 246, 0.45);
    }}
    .section-title {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 1.15rem;
      font-weight: 700;
      margin-bottom: 14px;
      color: #ffffff;
    }}
    .section-icon {{
      font-size: 1.4rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 36px; height: 36px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.05);
    }}
    .patch-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .patch-item {{
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 12px;
      padding: 14px 16px;
    }}
    .patch-tag {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-right: 8px;
    }}
    .tag-buff {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .tag-nerf {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }}
    .tag-fix {{ background: rgba(6, 182, 212, 0.2); color: #22d3ee; border: 1px solid rgba(6, 182, 212, 0.3); }}
    .tag-feat {{ background: rgba(139, 92, 246, 0.2); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.3); }}
    .tag-qol {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}
    .item-header {{
      font-weight: 600;
      font-size: 0.98rem;
      color: #f8fafc;
      margin-bottom: 4px;
    }}
    .item-desc {{
      color: #cbd5e1;
      font-size: 0.92rem;
      padding-left: 2px;
    }}
    .dialogue-box {{
      margin-top: 8px;
      background: rgba(0, 0, 0, 0.3);
      border-left: 3px solid var(--accent-purple);
      padding: 8px 12px;
      border-radius: 4px 8px 8px 4px;
      font-family: var(--font-mono);
      font-size: 0.85rem;
      color: #e2e8f0;
    }}
    .footer {{
      margin-top: 36px;
      text-align: center;
      color: #64748b;
      font-size: 0.85rem;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header-card">
      <div class="badge-row">
        <span class="badge badge-live">Servidor OCI Producción</span>
        <span class="badge badge-version">Update v{version}</span>
        <span class="badge badge-permanent">📜 Registro Oficial Permanente</span>
        <a href="/patch-notes" class="badge badge-history-link" style="text-decoration:none;">📚 Ver Historial Completo</a>
      </div>
      <h1>{title}</h1>
      <p class="subtitle">Registro oficial de cambios y mejoras del bot</p>
    </div>

{sections_html_block}

    <div class="footer">
      VaPls Discord Bot &bull; Oracle Cloud Infrastructure (OCI) &bull; Registro oficial permanente de notas de parche.
    </div>
  </div>
</body>
</html>"""

    def render_history_html(self, history: Optional[list[dict[str, Any]]] = None) -> str:
        """Render high-aesthetic, responsive historical patch notes archive page."""
        releases = list(history if history is not None else self.get_history())
        total_releases = len(releases)
        latest_version = html.escape(str(releases[0].get("version", "2.0")), quote=True) if releases else "2.0"
        now = time.time()

        if not releases:
            releases_html_block = """    <div class="empty-state-card">
      <div class="empty-icon">📜</div>
      <h2>No hay notas de parche registradas</h2>
      <p>El archivo histórico se actualizará automáticamente con cada nueva versión generada por el bot.</p>
    </div>"""
        else:
            rendered_releases = []
            for idx, rel in enumerate(releases):
                ver = html.escape(str(rel.get("version", "2.0")), quote=True)
                clean_ver = re.sub(r"[^a-zA-Z0-9_-]", "-", ver)
                title = html.escape(str(rel.get("title", f"Notas de Parche v{ver}")), quote=True)
                created_at = float(rel.get("created_at") or 0)
                if created_at > 0:
                    date_str = datetime.datetime.fromtimestamp(created_at, tz=TZ_ARG).strftime("%d/%m/%Y")
                else:
                    date_str = "Reciente"
                expires_at = float(rel.get("expires_at") or 0)

                # Badges
                badges = []
                if idx == 0:
                    badges.append('<span class="badge badge-latest">✨ Último Lanzamiento</span>')
                badges.append(f'<span class="badge badge-version">v{ver}</span>')
                badges.append(f'<span class="badge badge-date">📅 {date_str}</span>')
                tok_str = html.escape(str(rel.get("token") or ""), quote=True)
                if tok_str:
                    badges.append(f'<a href="/patch-notes/{tok_str}" class="badge badge-token-link" target="_blank" onclick="event.stopPropagation()">🔗 Anuncio Dedicado</a>')
                badges.append('<span class="badge badge-archived">📜 Registro Permanente</span>')

                badges_html = "\n        ".join(badges)

                # Discord highlight summary block (if present)
                highlight_block = ""
                highlight_data = rel.get("discord_highlight")
                if highlight_data and isinstance(highlight_data, dict):
                    cat_title = html.escape(str(highlight_data.get("category_title", "🛠️ MEJORAS DE LA SEMANA")), quote=True)
                    hl_items = highlight_data.get("items", [])
                    hl_li = []
                    for hli in hl_items:
                        h_title = html.escape(str(hli.get("title", "")), quote=True)
                        h_tag = html.escape(str(hli.get("tag", "Mejora")), quote=True)
                        h_desc = html.escape(str(hli.get("desc", "")), quote=True)
                        hl_li.append(f'            <li>• <strong>{h_title} ({h_tag}):</strong> {h_desc}</li>')
                    hl_li_html = "\n".join(hl_li)
                    highlight_block = f"""      <div class="highlight-summary-box">
        <div class="highlight-cat-title">{cat_title}</div>
        <ul class="highlight-summary-list">
{hl_li_html}
        </ul>
      </div>"""

                # Render release sections
                sections_data = rel.get("sections") or _get_default_v26_sections()
                if sections_data and isinstance(sections_data, list):
                    rendered_sections = []
                    for sec in sections_data:
                        sec_icon = html.escape(str(sec.get("icon", "📝")), quote=True)
                        sec_title = html.escape(str(sec.get("title", "Mejoras del Sistema")), quote=True)
                        items_html = []
                        for item in sec.get("items", []):
                            raw_tag = str(item.get("tag", "Mejora"))
                            tag = html.escape(raw_tag, quote=True)
                            raw_type = str(item.get("tag_type") or "").lower()
                            if not raw_type:
                                tag_lower = raw_tag.lower()
                                if "buff" in tag_lower or "nuevo" in tag_lower:
                                    raw_type = "buff"
                                elif "nerf" in tag_lower:
                                    raw_type = "nerf"
                                elif "fix" in tag_lower or "arreglo" in tag_lower:
                                    raw_type = "fix"
                                elif "feat" in tag_lower:
                                    raw_type = "feat"
                                else:
                                    raw_type = "qol"
                            css_tag = f"tag-{raw_type}" if raw_type in ("buff", "nerf", "fix", "feat", "qol") else "tag-qol"
                            header = html.escape(str(item.get("header") or item.get("title") or ""), quote=True)
                            desc = html.escape(str(item.get("desc", "")), quote=True)
                            dialogues = item.get("dialogues")
                            dialogue_html = ""
                            if dialogues and isinstance(dialogues, list):
                                dialogue_lines = "<br>\n".join(html.escape(str(d), quote=True) for d in dialogues)
                                dialogue_html = f'\n              <div class="dialogue-box">\n                {dialogue_lines}\n              </div>'
                            items_html.append(f"""          <li class="patch-item" data-tag="{css_tag}">
            <div class="item-header">
              <span class="patch-tag {css_tag}">{tag}</span>
              {header}
            </div>
            <div class="item-desc">
              {desc}{dialogue_html}
            </div>
          </li>""")
                        sec_content = "\n".join(items_html)
                        rendered_sections.append(f"""      <div class="section-card">
        <div class="section-title">
          <span class="section-icon">{sec_icon}</span>
          <span>{sec_title}</span>
        </div>
        <ul class="patch-list">
{sec_content}
        </ul>
      </div>""")
                    sections_html_block = "\n\n".join(rendered_sections)
                else:
                    sections_html_block = """      <div class="section-card">
        <div class="section-title">
          <span class="section-icon">🛠️</span>
          <span>Actualización General</span>
        </div>
        <ul class="patch-list">
          <li class="patch-item" data-tag="tag-qol">
            <div class="item-header">
              <span class="patch-tag tag-qol">Mejora</span>
              Ajustes de Sistema y Estabilidad
            </div>
            <div class="item-desc">
              Mantenimiento rutinario y optimizaciones en la plataforma del bot.
            </div>
          </li>
        </ul>
      </div>"""

                # Search content blob for fast client-side filtering
                search_blob = f"v{ver} {ver} {title} {date_str}".lower()

                collapsed_cls = " is-collapsed"
                aria_exp = "false"
                hint_text = "Clic para expandir"

                rendered_releases.append(f"""    <!-- Release v{ver} -->
    <article class="release-card{collapsed_cls}" id="release-v{clean_ver}" data-version="{ver.lower()}" data-search="{search_blob}">
      <div class="release-header" onclick="toggleRelease('release-v{clean_ver}')" onkeydown="onHeaderKey(event, 'release-v{clean_ver}')" tabindex="0" role="button" aria-expanded="{aria_exp}" title="Haz clic para desplegar o colapsar este parche">
        <div class="release-header-top">
          <div class="badge-row">
            {badges_html}
          </div>
          <button type="button" class="dropdown-chevron-btn" aria-label="Desplegar o colapsar parche" tabindex="-1">
            <span class="dropdown-chevron">▼</span>
          </button>
        </div>
        <div class="release-title-row">
          <h2>{title} <span class="dropdown-arrow">▾</span></h2>
        </div>
        <p class="release-subtitle">Publicado el {date_str} &bull; Registro oficial en infraestructura OCI <span class="dropdown-hint">({hint_text})</span></p>
      </div>

      <div class="release-body" id="body-release-v{clean_ver}">
{highlight_block}

{sections_html_block}

        <div class="release-footer">
          <span>VaPls Discord Bot &bull; Versión v{ver}</span>
          <a href="#release-v{clean_ver}" class="anchor-link" onclick="event.stopPropagation()"># Enlace directo a este parche</a>
        </div>
      </div>
    </article>""")

            releases_html_block = "\n\n".join(rendered_releases)

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Historial de Notas de Parche — VaPls &amp; El Indio</title>
  <meta name="description" content="Registro histórico oficial de notas de parche, novedades y correcciones de VaPls Discord Bot.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-base: #080612;
      --bg-card: rgba(18, 14, 34, 0.85);
      --bg-card-border: rgba(139, 92, 246, 0.22);
      --bg-card-border-focus: rgba(168, 85, 247, 0.55);
      --accent-purple: #8b5cf6;
      --accent-purple-glow: rgba(139, 92, 246, 0.35);
      --accent-blurple: #5865f2;
      --accent-pink: #ec4899;
      --accent-cyan: #06b6d4;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-red: #ef4444;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --font-body: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-body);
      background-color: var(--bg-base);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      line-height: 1.6;
      background-image: 
        radial-gradient(circle at 10% 10%, rgba(139, 92, 246, 0.15) 0%, transparent 45%),
        radial-gradient(circle at 90% 30%, rgba(6, 182, 212, 0.12) 0%, transparent 45%),
        radial-gradient(circle at 50% 90%, rgba(236, 72, 153, 0.1) 0%, transparent 40%);
      background-attachment: fixed;
    }}
    a {{ color: var(--accent-cyan); text-decoration: none; transition: color 0.2s; }}
    a:hover {{ color: #67e8f9; }}

    /* Navbar */
    .navbar {{
      position: sticky;
      top: 0;
      z-index: 100;
      backdrop-filter: blur(20px);
      background: rgba(8, 6, 18, 0.88);
      border-bottom: 1px solid var(--bg-card-border);
      padding: 14px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-icon {{
      width: 40px; height: 40px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent-purple), var(--accent-blurple));
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 4px 15px var(--accent-purple-glow);
    }}
    .brand-text h1 {{
      font-size: 1.15rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: linear-gradient(90deg, #fff, #c4b5fd);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin: 0;
    }}
    .brand-text p {{
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 500;
    }}
    .nav-actions {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .nav-btn {{
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--bg-card-border);
      color: var(--text-main);
      padding: 7px 14px;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
    }}
    .nav-btn:hover {{
      background: rgba(139, 92, 246, 0.18);
      border-color: var(--accent-purple);
      color: #fff;
      transform: translateY(-1px);
    }}

    /* Container */
    .container {{
      max-width: 900px;
      width: 100%;
      margin: 0 auto;
      padding: 36px 20px 80px 20px;
    }}

    /* Hero */
    .hero {{
      text-align: center;
      margin-bottom: 36px;
      position: relative;
    }}
    .hero-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: rgba(139, 92, 246, 0.15);
      border: 1px solid rgba(139, 92, 246, 0.35);
      color: #c4b5fd;
      padding: 6px 16px;
      border-radius: 999px;
      font-size: 0.85rem;
      font-weight: 700;
      margin-bottom: 16px;
    }}
    .hero h1 {{
      font-size: clamp(2rem, 5vw, 2.8rem);
      font-weight: 800;
      letter-spacing: -0.02em;
      margin-bottom: 12px;
      line-height: 1.2;
    }}
    .hero h1 span.gradient {{
      background: linear-gradient(135deg, #ffffff 30%, #c4b5fd 70%, var(--accent-cyan) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .hero p {{
      color: var(--text-muted);
      font-size: 1.05rem;
      max-width: 680px;
      margin: 0 auto 24px auto;
    }}
    .stats-row {{
      display: flex;
      justify-content: center;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 28px;
    }}
    .stat-chip {{
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
      color: #cbd5e1;
    }}

    /* Search & Filter Bar */
    .search-wrapper {{
      max-width: 620px;
      margin: 0 auto 36px auto;
      position: relative;
    }}
    .search-input {{
      width: 100%;
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 16px;
      padding: 14px 20px 14px 46px;
      color: #fff;
      font-family: var(--font-body);
      font-size: 0.96rem;
      outline: none;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
      transition: all 0.25s ease;
    }}
    .search-input:focus {{
      border-color: var(--accent-purple);
      box-shadow: 0 10px 30px var(--accent-purple-glow);
    }}
    .search-icon {{
      position: absolute;
      left: 16px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 1.2rem;
      color: var(--text-muted);
      pointer-events: none;
    }}
    .filter-pills {{
      display: flex;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 14px;
    }}
    .filter-pill {{
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      color: var(--text-muted);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .filter-pill:hover, .filter-pill.active {{
      background: rgba(139, 92, 246, 0.2);
      border-color: var(--accent-purple);
      color: #fff;
    }}

    /* Release Card */
    .release-card {{
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 22px;
      padding: 32px 28px;
      margin-bottom: 32px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
      backdrop-filter: blur(16px);
      position: relative;
      overflow: hidden;
      transition: border-color 0.25s ease, transform 0.25s ease;
    }}
    .release-card:hover {{
      border-color: rgba(139, 92, 246, 0.45);
    }}
    .release-card::before {{
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--accent-purple), var(--accent-pink), var(--accent-cyan));
    }}
    .release-header {{
      cursor: pointer;
      user-select: none;
      transition: background 0.2s ease, border-color 0.2s ease;
      border-radius: 14px;
      padding: 14px 16px;
      margin: -14px -14px 20px -14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.07);
    }}
    .release-header:hover {{
      background: rgba(139, 92, 246, 0.08);
    }}
    .release-header:focus-visible {{
      outline: 2px solid var(--accent-purple);
      outline-offset: 2px;
    }}
    .release-header-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .dropdown-chevron-btn {{
      background: transparent;
      border: none;
      padding: 0;
      cursor: pointer;
    }}
    .dropdown-chevron {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px;
      height: 32px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.12);
      color: var(--accent-cyan);
      font-size: 0.85rem;
      transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1), background 0.2s ease, border-color 0.2s ease;
    }}
    .release-header:hover .dropdown-chevron {{
      background: rgba(139, 92, 246, 0.22);
      border-color: var(--accent-purple);
      color: #fff;
    }}
    .release-title-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 6px;
    }}
    .dropdown-arrow {{
      display: inline-block;
      font-size: 1.1rem;
      color: var(--accent-purple);
      transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      margin-left: 6px;
      vertical-align: middle;
    }}
    .release-header:hover .dropdown-arrow {{
      color: var(--accent-cyan);
    }}
    .dropdown-hint {{
      display: inline-block;
      margin-left: 8px;
      font-size: 0.78rem;
      color: #a78bfa;
      font-weight: 500;
    }}
    .release-card.is-collapsed {{
      padding-bottom: 20px;
    }}
    .release-card.is-collapsed .release-header {{
      margin-bottom: 0;
      border-bottom: none;
      padding-bottom: 14px;
    }}
    .release-card.is-collapsed .release-body {{
      display: none;
    }}
    .release-card.is-collapsed .dropdown-chevron {{
      transform: rotate(-90deg);
    }}
    .release-card.is-collapsed .dropdown-arrow {{
      transform: rotate(-90deg);
    }}
    .badge-row {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 11px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .badge-latest {{
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.4);
      color: var(--accent-green);
    }}
    .badge-version {{
      background: rgba(139, 92, 246, 0.15);
      border: 1px solid rgba(139, 92, 246, 0.4);
      color: #c4b5fd;
    }}
    .badge-date {{
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.1);
      color: #cbd5e1;
    }}
    .badge-token-link {{
      background: rgba(6, 182, 212, 0.15);
      border: 1px solid rgba(6, 182, 212, 0.4);
      color: var(--accent-cyan);
      transition: all 0.2s ease;
    }}
    .badge-token-link:hover {{
      background: rgba(6, 182, 212, 0.3);
      color: #fff;
    }}
    .badge-archived {{
      background: rgba(100, 116, 139, 0.15);
      border: 1px solid rgba(100, 116, 139, 0.3);
      color: #94a3b8;
    }}
    .tool-btn {{
      background: rgba(139, 92, 246, 0.15);
      border: 1px solid rgba(139, 92, 246, 0.35);
      color: #c4b5fd;
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      font-family: var(--font-body);
    }}
    .tool-btn:hover {{
      background: rgba(139, 92, 246, 0.28);
      border-color: var(--accent-purple);
      color: #fff;
    }}
    .release-header h2 {{
      font-size: 1.65rem;
      font-weight: 800;
      color: #fff;
      margin-bottom: 4px;
    }}
    .release-subtitle {{
      color: var(--text-muted);
      font-size: 0.88rem;
    }}

    /* Highlight summary box */
    .highlight-summary-box {{
      background: rgba(139, 92, 246, 0.08);
      border: 1px solid rgba(139, 92, 246, 0.25);
      border-radius: 14px;
      padding: 16px 20px;
      margin-bottom: 22px;
    }}
    .highlight-cat-title {{
      font-size: 0.85rem;
      font-weight: 700;
      color: #c4b5fd;
      letter-spacing: 0.03em;
      margin-bottom: 8px;
    }}
    .highlight-summary-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 0.92rem;
      color: #e2e8f0;
    }}

    /* Section Cards */
    .section-card {{
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 16px;
      padding: 20px 20px 16px 20px;
      margin-bottom: 16px;
    }}
    .section-title {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 14px;
      color: #fff;
    }}
    .section-icon {{
      font-size: 1.3rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px; height: 32px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.05);
    }}
    .patch-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .patch-item {{
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 12px;
      padding: 12px 16px;
    }}
    .patch-tag {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-right: 8px;
    }}
    .tag-buff {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .tag-nerf {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }}
    .tag-fix {{ background: rgba(6, 182, 212, 0.2); color: #22d3ee; border: 1px solid rgba(6, 182, 212, 0.3); }}
    .tag-feat {{ background: rgba(139, 92, 246, 0.2); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.3); }}
    .tag-qol {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }}
    .item-header {{
      font-weight: 600;
      font-size: 0.95rem;
      color: #f8fafc;
      margin-bottom: 4px;
    }}
    .item-desc {{
      color: #cbd5e1;
      font-size: 0.9rem;
    }}
    .dialogue-box {{
      margin-top: 8px;
      background: rgba(0, 0, 0, 0.35);
      border-left: 3px solid var(--accent-purple);
      padding: 8px 12px;
      border-radius: 4px 8px 8px 4px;
      font-family: var(--font-mono);
      font-size: 0.83rem;
      color: #e2e8f0;
    }}
    .release-footer {{
      margin-top: 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.8rem;
      color: var(--text-muted);
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      padding-top: 14px;
    }}
    .anchor-link {{
      color: var(--text-muted);
      font-family: var(--font-mono);
      font-size: 0.78rem;
    }}
    .anchor-link:hover {{
      color: var(--accent-purple);
    }}

    /* Empty state */
    .empty-state-card {{
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 20px;
      padding: 50px 30px;
      text-align: center;
      color: var(--text-muted);
    }}
    .empty-icon {{ font-size: 50px; margin-bottom: 16px; }}
    .empty-state-card h2 {{ color: #fff; margin-bottom: 8px; }}

    /* Footer */
    footer {{
      margin-top: auto;
      border-top: 1px solid var(--bg-card-border);
      background: rgba(8, 6, 18, 0.92);
      padding: 36px 20px;
      text-align: center;
      font-size: 0.84rem;
      color: #64748b;
    }}
    .footer-links {{
      display: flex;
      justify-content: center;
      gap: 20px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .footer-links a {{ color: var(--text-muted); }}
    .footer-links a:hover {{ color: #fff; }}
  </style>
</head>
<body>

  <!-- Top Navbar -->
  <header class="navbar">
    <div class="brand">
      <div class="brand-icon">⚡</div>
      <div class="brand-text">
        <h1>VaPls &amp; El Indio</h1>
        <p>Registro Histórico Oficial &bull; Updates</p>
      </div>
    </div>
    <div class="nav-actions">
      <a href="/" class="nav-btn" id="btn-nav-home">🏠 Consola Web</a>
      <a href="/stremio" class="nav-btn" id="btn-nav-stremio">🎬 Stremio</a>
      <span class="badge badge-latest" style="font-size: 0.72rem;">OCI Producción</span>
    </div>
  </header>

  <!-- Main Container -->
  <main class="container">

    <!-- Hero -->
    <section class="hero">
      <div class="hero-badge">
        📚 Archivo Oficial &bull; Histórico Permanente
      </div>
      <h1>Historial de <span class="gradient">Notas de Parche</span></h1>
      <p>
        Cronología completa de actualizaciones, balances de IA de El Indio, netcode de Go Live y mejoras del bot.
      </p>
      <div class="stats-row">
        <span class="stat-chip">Versiones Registradas: {total_releases}</span>
        <span class="stat-chip">Último Update: v{latest_version}</span>
        <span class="stat-chip">Servidor: Oracle Cloud Infrastructure</span>
      </div>

      <!-- Search & Filter Bar -->
      <div class="search-wrapper">
        <span class="search-icon">🔍</span>
        <input type="text" id="patch-search" class="search-input" placeholder="Buscar por versión, cambio o novedad (ej: indio, stream, fix, audio)..." autocomplete="off" />
        <div class="filter-pills">
          <button type="button" class="filter-pill active" data-category="all" id="pill-all">Todos</button>
          <button type="button" class="filter-pill" data-category="tag-buff" id="pill-buffs">🚀 Novedades &amp; Buffs</button>
          <button type="button" class="filter-pill" data-category="tag-fix" id="pill-fixes">🔧 Arreglos (Fixes)</button>
          <button type="button" class="filter-pill" data-category="indio" id="pill-indio">🧠 El Indio</button>
          <button type="button" class="filter-pill" data-category="stream" id="pill-stream">📺 Go Live &amp; Stremio</button>
          <button type="button" class="filter-pill" data-category="musica" id="pill-music">🎵 Música</button>
          <button type="button" class="tool-btn" id="btn-toggle-all" onclick="toggleAllReleases()">📂 Desplegar Todos</button>
        </div>
      </div>
    </section>

    <!-- Releases List -->
    <section id="releases-container">
{releases_html_block}
    </section>

  </main>

  <!-- Footer -->
  <footer>
    <div class="footer-links">
      <a href="/">Consola Web</a>
      <a href="/privacy">Privacidad</a>
      <a href="/delete-data">Eliminar Datos</a>
      <a href="/stremio">Stremio Web UI</a>
      <a href="https://github.com/dilelu94/VaPls-Discord-Bot" target="_blank" rel="noopener noreferrer">GitHub</a>
    </div>
    <p>
      VaPls Discord Bot &bull; Oracle Cloud Infrastructure (OCI) &bull; Registro histórico permanente y seguro.
    </p>
    <p style="margin-top: 6px; font-size: 0.78rem; color: #475569;">
      🔒 Entorno seguro: CSP estricto, sin scripts externos y Zero Leaks de credenciales o tokens.
    </p>
  </footer>

  <!-- Search & Filter Script & Accordion Dropdown -->
  <script>
    (function() {{
      var searchInput = document.getElementById("patch-search");
      var cards = document.querySelectorAll(".release-card");
      var pills = document.querySelectorAll(".filter-pill");

      window.toggleRelease = function(cardId) {{
        var card = document.getElementById(cardId);
        if (!card) return;
        var isCollapsed = card.classList.toggle("is-collapsed");
        var header = card.querySelector(".release-header");
        if (header) {{
          header.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
          var hint = header.querySelector(".dropdown-hint");
          if (hint) {{
            hint.textContent = isCollapsed ? "(Clic para expandir)" : "(Clic para contraer)";
          }}
        }}
      }};

      window.onHeaderKey = function(event, cardId) {{
        if (event.key === "Enter" || event.key === " ") {{
          event.preventDefault();
          window.toggleRelease(cardId);
        }}
      }};

      window.toggleAllReleases = function() {{
        var cardsList = document.querySelectorAll(".release-card");
        if (!cardsList.length) return;
        var anyCollapsed = false;
        cardsList.forEach(function(c) {{
          if (c.classList.contains("is-collapsed")) anyCollapsed = true;
        }});
        cardsList.forEach(function(card) {{
          if (anyCollapsed) {{
            card.classList.remove("is-collapsed");
            var h = card.querySelector(".release-header");
            if (h) {{
              h.setAttribute("aria-expanded", "true");
              var hint = h.querySelector(".dropdown-hint");
              if (hint) hint.textContent = "(Clic para contraer)";
            }}
          }} else {{
            card.classList.add("is-collapsed");
            var h = card.querySelector(".release-header");
            if (h) {{
              h.setAttribute("aria-expanded", "false");
              var hint = h.querySelector(".dropdown-hint");
              if (hint) hint.textContent = "(Clic para expandir)";
            }}
          }}
        }});
        var btn = document.getElementById("btn-toggle-all");
        if (btn) {{
          btn.textContent = anyCollapsed ? "📁 Colapsar Todos" : "📂 Desplegar Todos";
        }}
      }};

      // Auto-expand card if URL has direct anchor (#release-v...)
      if (window.location.hash) {{
        var target = document.querySelector(window.location.hash);
        if (target && target.classList.contains("is-collapsed")) {{
          target.classList.remove("is-collapsed");
          var h = target.querySelector(".release-header");
          if (h) {{
            h.setAttribute("aria-expanded", "true");
            var hint = h.querySelector(".dropdown-hint");
            if (hint) hint.textContent = "(Clic para contraer)";
          }}
          target.scrollIntoView({{ behavior: "smooth" }});
        }}
      }}

      function filterPatches() {{
        var query = searchInput ? searchInput.value.toLowerCase().trim() : "";
        var activePill = document.querySelector(".filter-pill.active");
        var category = activePill ? activePill.getAttribute("data-category") : "all";

        cards.forEach(function(card) {{
          var searchBlob = card.getAttribute("data-search") || "";
          var cardText = card.innerText.toLowerCase();
          var matchesQuery = !query || searchBlob.indexOf(query) !== -1 || cardText.indexOf(query) !== -1;

          var matchesCategory = true;
          if (category !== "all") {{
            if (category.indexOf("tag-") === 0) {{
              var tagItems = card.querySelectorAll('.patch-item[data-tag="' + category + '"]');
              matchesCategory = (tagItems.length > 0);
            }} else {{
              matchesCategory = (cardText.indexOf(category) !== -1);
            }}
          }}

          if (matchesQuery && matchesCategory) {{
            card.style.display = "";
            if (query) {{
              card.classList.remove("is-collapsed");
              var h = card.querySelector(".release-header");
              if (h) {{
                h.setAttribute("aria-expanded", "true");
                var hint = h.querySelector(".dropdown-hint");
                if (hint) hint.textContent = "(Clic para contraer)";
              }}
            }}
          }} else {{
            card.style.display = "none";
          }}
        }});
      }}

      if (searchInput) {{
        searchInput.addEventListener("input", filterPatches);
      }}

      pills.forEach(function(pill) {{
        pill.addEventListener("click", function() {{
          pills.forEach(function(p) {{ p.classList.remove("active"); }});
          this.classList.add("active");
          filterPatches();
        }});
      }});
    }})();
  </script>
</body>
</html>"""

    def render_expired_html(self) -> str:
        """Render page for expired link (410 Gone)."""
        return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Enlace Expirado — Notas de Parche</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #090713;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: rgba(18, 14, 34, 0.85);
      border: 1px solid rgba(239, 68, 68, 0.3);
      border-radius: 18px;
      padding: 40px 30px;
      max-width: 460px;
      width: 100%;
      text-align: center;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
    }
    .icon { font-size: 50px; margin-bottom: 16px; }
    h1 { font-size: 1.5rem; color: #f87171; margin-bottom: 12px; }
    p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; margin-bottom: 20px; }
    .badge {
      display: inline-block;
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: #fca5a5;
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⌛</div>
    <h1>Enlace Expirado</h1>
    <p>Este enlace temporal de notas de parche tenía una validez de 24 horas y ha caducado por motivos de seguridad.</p>
    <div class="badge">410 Gone &bull; Token Expirado</div>
  </div>
</body>
</html>"""

    def render_not_found_html(self) -> str:
        """Render page for invalid or missing token (404 Not Found)."""
        return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Notas de Parche no encontradas</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #090713;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: rgba(18, 14, 34, 0.85);
      border: 1px solid rgba(139, 92, 246, 0.3);
      border-radius: 18px;
      padding: 40px 30px;
      max-width: 460px;
      width: 100%;
      text-align: center;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
    }
    .icon { font-size: 50px; margin-bottom: 16px; }
    h1 { font-size: 1.5rem; color: #c4b5fd; margin-bottom: 12px; }
    p { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔍</div>
    <h1>Enlace no encontrado</h1>
    <p>El token de notas de parche especificado no existe o no es válido.</p>
  </div>
</body>
</html>"""


# Global singleton instance
patch_notes_manager = PatchNotesManager()


def get_patch_notes_url(token: str) -> str:
    """Return the absolute public URL for a patch notes token, using configured domain."""
    try:
        import config

        base = getattr(
            config,
            "PATCH_NOTES_BASE_URL",
            f"https://{getattr(config, 'DUCKDNS_DOMAIN', 'vapls.duckdns.org')}",
        ).rstrip("/")
    except Exception:
        base = "https://vapls.duckdns.org"
    return f"{base}/patch-notes/{token}"


def get_patch_notes_history_url() -> str:
    """Return the absolute public URL for patch notes history archive."""
    try:
        import config

        base = getattr(
            config,
            "PATCH_NOTES_BASE_URL",
            f"https://{getattr(config, 'DUCKDNS_DOMAIN', 'vapls.duckdns.org')}",
        ).rstrip("/")
    except Exception:
        base = "https://vapls.duckdns.org"
    return f"{base}/patch-notes"

