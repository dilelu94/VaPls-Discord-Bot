"""Patch Notes Web UI and token management for VaPls Discord Bot.

Provides secure, time-expiring (24h) web pages to display full patch notes
with strict security headers (CSP, anti-framing, no-referrer), IP rate-limiting,
cryptographic token generation, and automatic expiration.
"""

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


class PatchNotesManager:
    """Manages secure, expiring tokens and HTML rendering for Patch Notes."""

    def __init__(self, data_path: str = DEFAULT_DATA_PATH):
        self.data_path = data_path
        self._tokens: dict[str, dict[str, Any]] = {}
        self._rate_limits: dict[str, list[float]] = {}
        self._load()

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

    def _cleanup_old_tokens(self) -> None:
        """Prune tokens expired more than 24 hours ago."""
        now = time.time()
        to_delete = [
            tok for tok, data in self._tokens.items()
            if now > data.get("expires_at", 0) + 86400
        ]
        for tok in to_delete:
            del self._tokens[tok]

    def create_token(
        self,
        version: str = "2.6",
        ttl_seconds: int = 86400,
        title: str = "Notas de Parche v2.6",
    ) -> str:
        """Generate a cryptographically secure token valid for `ttl_seconds` (default 24h)."""
        self._cleanup_old_tokens()
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._tokens[token] = {
            "token": token,
            "version": version,
            "title": title,
            "created_at": now,
            "expires_at": now + ttl_seconds,
        }
        self._save()
        logger.info("Created patch notes token %s (version=%s, ttl=%ds)", token[:8], version, ttl_seconds)
        return token

    def get_token_status(self, token: Optional[str]) -> tuple[str, Optional[dict[str, Any]]]:
        """Validate token and return (status, data).

        Status is one of: 'valid', 'expired', 'not_found', 'invalid_format'.
        """
        if not token or not isinstance(token, str) or not TOKEN_REGEX.match(token):
            return "invalid_format", None

        data = self._tokens.get(token)
        if not data:
            return "not_found", None

        now = time.time()
        if now > data.get("expires_at", 0):
            return "expired", data

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
        expires_at = int(token_data.get("expires_at", time.time() + 86400))
        remaining_secs = max(0, int(expires_at - time.time()))
        rem_hours = remaining_secs // 3600
        rem_minutes = (remaining_secs % 3600) // 60

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
    .badge-timer {{
      background: rgba(245, 158, 11, 0.15);
      border: 1px solid rgba(245, 158, 11, 0.4);
      color: #fcd34d;
      font-family: var(--font-mono);
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
        <span class="badge badge-timer" id="countdown">⏳ Expira en {rem_hours}h {rem_minutes}m</span>
      </div>
      <h1>{title}</h1>
      <p class="subtitle">Registro oficial de cambios, balance de combate y parches del sistema</p>
    </div>

    <!-- 1. ANTI-EXPLOITS & LOOT -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🛡️</span>
        <span>Anti-Exploits & Sistema de Loot</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-nerf">Nerf / Anti-Farm</span>
            Pity de Saludos Raros (Cooldown Interno)
          </div>
          <div class="item-desc">
            Se implementó un <strong>cooldown de 1 hora</strong> para el incremento de pity por usuario. Se cancela el exploit de entrar y salir repetidamente del canal de voz para farmear audios raros (como el Don Cangrejo de Seba).
          </div>
        </li>
      </ul>
    </div>

    <!-- 2. NETCODE & GOLIVE -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">📺</span>
        <span>Netcode & GoLive Streaming</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-buff">Buff / Netcode</span>
            Sincronización Audio / Video (A/V Sync)
          </div>
          <div class="item-desc">
            Se eliminó el desfasaje en transmisiones GoLive (IPTV, HLS y Stremio). Ahora el inicio de audio se sincroniza de forma precisa con la emisión del primer fotograma (<code>first_frame_sent</code>) con aislamiento de flags por entrada en FFmpeg.
          </div>
        </li>
      </ul>
    </div>

    <!-- 3. BALANCE DE PERSONAJE — EL INDIO -->
    <div class="section-card">
      <div class="section-title">
        <span class="section-icon">🧠</span>
        <span>Balance de Personaje — El Indio</span>
      </div>
      <ul class="patch-list">
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-buff">Buff</span>
            Habilidades Verbales & Frases Cruzadas
          </div>
          <div class="item-desc">
            Se agregaron nuevos remates verbales y provocaciones al repertorio dialéctico del Indio Cruzado:
            <div class="dialogue-box">
              💬 "andá a hacerte ortear"<br>
              💬 "¿por qué no me sopapeás la papirola?"
            </div>
          </div>
        </li>
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-fix">Fix / Precisión</span>
            Puntería de Berretines ('cabecear el enano')
          </div>
          <div class="item-desc">
            Se corrigió la direccionalidad e intención del prompt para asegurar que las bardeadas se dirijan estrictamente hacia el contrincante y no de forma autodirigida.
          </div>
        </li>
        <li class="patch-item">
          <div class="item-header">
            <span class="patch-tag tag-fix">Fix</span>
            Seguro de Gatillo (Anti-Misfire en Controles de Reproducción)
          </div>
          <div class="item-desc">
            Se añadieron compuertas de seguridad para que el Indio no dispare comandos de pausa o cambio de música por confusión al chatear espontáneamente.
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
    </div>

    <div class="footer">
      VaPls Discord Bot &bull; Oracle Cloud Infrastructure (OCI) &bull; Este enlace temporal vence automáticamente en 24 horas.
    </div>
  </div>

  <script>
    (function() {{
      var expiresAt = {expires_at};
      function updateTimer() {{
        var now = Math.floor(Date.now() / 1000);
        var diff = expiresAt - now;
        var el = document.getElementById("countdown");
        if (!el) return;
        if (diff <= 0) {{
          el.innerText = "⏳ Enlace expirado";
          el.style.color = "#f87171";
          return;
        }}
        var h = Math.floor(diff / 3600);
        var m = Math.floor((diff % 3600) / 60);
        var s = diff % 60;
        el.innerText = "⏳ Expira en " + h + "h " + (m < 10 ? "0" : "") + m + "m " + (s < 10 ? "0" : "") + s + "s";
      }}
      setInterval(updateTimer, 1000);
      updateTimer();
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
