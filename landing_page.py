"""Landing Page Web UI and Command Catalog for VaPls Discord Bot.

Provides a rich, interactive, dark-mode control console at https://vapls.duckdns.org/
showcasing all bot capabilities (GoLive screenshare streaming, music playback,
soundpad, El Indio AI, virtual pet, trivia, transfer portal, etc.).

Security Policy:
- Completely public and read-only.
- Zero server-side execution of bot commands via HTTP.
- Zero sensitive data or credentials leaked in HTML/JS.
- Strict security headers (CSP, anti-clickjacking, nosniff).
- Interactive controls simulate command generation and guide users with the exact
  slash command to run inside Discord.
"""

import html
import logging
import time
from typing import Optional
from aiohttp import web

logger = logging.getLogger("vapls.landing_page")


class LandingPageManager:
    """Manages rate-limiting, security headers, and HTML rendering for the public landing page."""

    def __init__(self) -> None:
        self._rate_limits: dict[str, list[float]] = {}

    def check_rate_limit(self, ip: str, limit: int = 120, window: float = 60.0) -> bool:
        """Check if IP has exceeded the request threshold within window seconds."""
        now = time.time()
        timestamps = self._rate_limits.setdefault(ip, [])
        self._rate_limits[ip] = [t for t in timestamps if now - t < window]
        if len(self._rate_limits[ip]) >= limit:
            return False
        self._rate_limits[ip].append(now)
        return True

    def add_security_headers(self, response: web.Response) -> web.Response:
        """Attach strict HTTP security headers."""
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

    def render_landing_page_html(self) -> str:
        """Render high-aesthetic interactive web console for VaPls Discord Bot."""
        return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>VaPls & El Indio — Centro de Control y Comandos de Discord</title>
  <meta name="description" content="Centro de control interactivo y catálogo de comandos de VaPls Discord Bot: Transmisión Go Live, música, soundpad, IA de El Indio y utilidades.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #07050f;
      --bg-card: rgba(16, 13, 31, 0.85);
      --bg-card-hover: rgba(23, 19, 44, 0.95);
      --bg-card-border: rgba(139, 92, 246, 0.22);
      --bg-card-border-focus: rgba(168, 85, 247, 0.55);
      --accent-blurple: #5865f2;
      --accent-purple: #8b5cf6;
      --accent-purple-glow: rgba(139, 92, 246, 0.4);
      --accent-pink: #ec4899;
      --accent-cyan: #06b6d4;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-red: #ef4444;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --font-body: 'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: var(--font-body);
      background-color: var(--bg-base);
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      line-height: 1.6;
      overflow-x: hidden;
      background-image: 
        radial-gradient(circle at 10% 10%, rgba(139, 92, 246, 0.15) 0%, transparent 45%),
        radial-gradient(circle at 90% 25%, rgba(6, 182, 212, 0.12) 0%, transparent 50%),
        radial-gradient(circle at 50% 90%, rgba(236, 72, 153, 0.1) 0%, transparent 45%);
      background-attachment: fixed;
    }

    a { color: var(--accent-cyan); text-decoration: none; transition: color 0.2s; }
    a:hover { color: #67e8f9; }

    /* Top Navigation */
    .navbar {
      position: sticky;
      top: 0;
      z-index: 100;
      backdrop-filter: blur(20px);
      background: rgba(7, 5, 15, 0.85);
      border-bottom: 1px solid var(--bg-card-border);
      padding: 14px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand-icon {
      width: 40px;
      height: 40px;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent-purple), var(--accent-blurple));
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 4px 15px var(--accent-purple-glow);
    }

    .brand-text h1 {
      font-size: 1.15rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: linear-gradient(90deg, #fff, #c4b5fd);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .brand-text p {
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 500;
    }

    .nav-actions {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.35);
      color: var(--accent-green);
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 700;
      letter-spacing: 0.03em;
    }

    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--accent-green);
      box-shadow: 0 0 8px var(--accent-green);
      animation: pulse-dot 2s infinite;
    }

    @keyframes pulse-dot {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
    }

    .nav-link-btn {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--bg-card-border);
      color: var(--text-main);
      padding: 7px 14px;
      border-radius: 10px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
    }

    .nav-link-btn:hover {
      background: rgba(139, 92, 246, 0.18);
      border-color: var(--accent-purple);
      color: #fff;
      transform: translateY(-1px);
    }

    /* Main Container */
    .container {
      max-width: 1200px;
      width: 100%;
      margin: 0 auto;
      padding: 32px 20px 80px 20px;
    }

    /* Hero Section */
    .hero {
      text-align: center;
      padding: 30px 10px 45px 10px;
      position: relative;
    }

    .hero-badge {
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
      margin-bottom: 20px;
    }

    .hero h2 {
      font-size: clamp(2rem, 5vw, 3.2rem);
      font-weight: 800;
      letter-spacing: -0.03em;
      line-height: 1.15;
      margin-bottom: 16px;
    }

    .hero h2 span.gradient {
      background: linear-gradient(135deg, #c4b5fd, var(--accent-blurple), var(--accent-cyan));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .hero p {
      max-width: 720px;
      margin: 0 auto 28px auto;
      color: var(--text-muted);
      font-size: 1.05rem;
    }

    /* Search & Filter Bar */
    .search-wrapper {
      max-width: 640px;
      margin: 0 auto 40px auto;
      position: relative;
    }

    .search-input {
      width: 100%;
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 16px;
      padding: 16px 20px 16px 50px;
      color: #fff;
      font-family: var(--font-body);
      font-size: 1rem;
      outline: none;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
      transition: all 0.25s ease;
    }

    .search-input:focus {
      border-color: var(--accent-purple);
      box-shadow: 0 10px 30px var(--accent-purple-glow);
    }

    .search-icon {
      position: absolute;
      left: 18px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 1.2rem;
      color: var(--text-muted);
      pointer-events: none;
    }

    .category-pills {
      display: flex;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 14px;
    }

    .pill-btn {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      color: var(--text-muted);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .pill-btn:hover, .pill-btn.active {
      background: rgba(139, 92, 246, 0.2);
      border-color: var(--accent-purple);
      color: #fff;
    }

    /* Module Grid */
    .modules-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
      gap: 24px;
    }

    .module-card {
      background: var(--bg-card);
      border: 1px solid var(--bg-card-border);
      border-radius: 20px;
      padding: 26px;
      backdrop-filter: blur(16px);
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.45);
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      gap: 18px;
      transition: transform 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease;
    }

    .module-card:hover {
      transform: translateY(-3px);
      border-color: var(--bg-card-border-focus);
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6);
    }

    .card-stripe {
      position: absolute;
      top: 0; left: 0; right: 0; height: 3px;
    }

    .card-stripe.stream { background: linear-gradient(90deg, #ec4899, #8b5cf6); }
    .card-stripe.music { background: linear-gradient(90deg, #8b5cf6, #06b6d4); }
    .card-stripe.ai { background: linear-gradient(90deg, #06b6d4, #10b981); }
    .card-stripe.soundpad { background: linear-gradient(90deg, #f59e0b, #ef4444); }
    .card-stripe.community { background: linear-gradient(90deg, #10b981, #f59e0b); }
    .card-stripe.patches { background: linear-gradient(90deg, #8b5cf6, #ec4899); }

    .module-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }

    .module-title-box {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .module-icon {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
    }

    .module-title-box h3 {
      font-size: 1.25rem;
      font-weight: 700;
      color: #fff;
    }

    .module-title-box p {
      font-size: 0.8rem;
      color: var(--text-muted);
    }

    .command-tag {
      font-family: var(--font-mono);
      font-size: 0.76rem;
      background: rgba(139, 92, 246, 0.16);
      border: 1px solid rgba(139, 92, 246, 0.35);
      color: #c4b5fd;
      padding: 4px 10px;
      border-radius: 8px;
      font-weight: 600;
      white-space: nowrap;
    }

    .module-desc {
      font-size: 0.88rem;
      color: #cbd5e1;
      line-height: 1.5;
    }

    /* Interactive Form Elements inside cards */
    .module-form {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 4px;
    }

    .input-field-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .input-label {
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .form-input {
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      padding: 11px 14px;
      color: #fff;
      font-family: var(--font-body);
      font-size: 0.92rem;
      outline: none;
      transition: all 0.2s ease;
    }

    .form-input:focus {
      border-color: var(--accent-purple);
      background: rgba(0, 0, 0, 0.5);
    }

    .form-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }

    /* Action Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 11px 18px;
      border-radius: 12px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: all 0.2s ease;
      font-family: var(--font-body);
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--accent-purple), var(--accent-blurple));
      color: #fff;
      box-shadow: 0 4px 15px rgba(139, 92, 246, 0.35);
    }

    .btn-primary:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(139, 92, 246, 0.5);
    }

    .btn-secondary {
      background: rgba(255, 255, 255, 0.07);
      border: 1px solid var(--bg-card-border);
      color: var(--text-main);
    }

    .btn-secondary:hover {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(255, 255, 255, 0.2);
    }

    .btn-danger {
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: #fca5a5;
    }

    .btn-danger:hover {
      background: rgba(239, 68, 68, 0.25);
    }

    /* Soundpad grid simulation */
    .soundpad-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 8px;
      margin-top: 6px;
    }

    .sound-btn {
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 10px;
      padding: 10px 12px;
      color: #e2e8f0;
      font-size: 0.84rem;
      font-weight: 500;
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .sound-btn:hover {
      background: rgba(245, 158, 11, 0.15);
      border-color: rgba(245, 158, 11, 0.4);
      color: #fef3c7;
      transform: translateY(-1px);
    }

    /* Command info footer in cards */
    .card-footer {
      border-top: 1px solid rgba(255, 255, 255, 0.07);
      padding-top: 14px;
      margin-top: auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.78rem;
      color: var(--text-dim);
    }

    .discord-badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: #a5b4fc;
      font-family: var(--font-mono);
    }

    /* Modal Styles */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(4, 3, 10, 0.85);
      backdrop-filter: blur(12px);
      z-index: 1000;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      opacity: 0;
      transition: opacity 0.25s ease;
    }

    .modal-overlay.active {
      display: flex;
      opacity: 1;
    }

    .modal-card {
      background: #0f0b21;
      border: 1px solid rgba(139, 92, 246, 0.4);
      border-radius: 24px;
      max-width: 540px;
      width: 100%;
      padding: 32px 28px;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.8), 0 0 40px rgba(139, 92, 246, 0.2);
      transform: scale(0.94);
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      position: relative;
    }

    .modal-overlay.active .modal-card {
      transform: scale(1);
    }

    .modal-close-btn {
      position: absolute;
      top: 20px;
      right: 20px;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.1);
      color: var(--text-muted);
      width: 32px;
      height: 32px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 1.1rem;
      transition: all 0.2s;
    }

    .modal-close-btn:hover {
      background: rgba(255, 255, 255, 0.15);
      color: #fff;
    }

    .modal-icon-header {
      width: 56px;
      height: 56px;
      border-radius: 16px;
      background: rgba(88, 101, 242, 0.18);
      border: 1px solid rgba(88, 101, 242, 0.4);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      margin-bottom: 20px;
      color: #818cf8;
    }

    .modal-card h3 {
      font-size: 1.45rem;
      font-weight: 800;
      color: #fff;
      margin-bottom: 8px;
    }

    .modal-card p.modal-desc {
      color: var(--text-muted);
      font-size: 0.92rem;
      line-height: 1.5;
      margin-bottom: 20px;
    }

    .security-notice-box {
      background: rgba(245, 158, 11, 0.1);
      border: 1px solid rgba(245, 158, 11, 0.3);
      border-radius: 12px;
      padding: 12px 14px;
      font-size: 0.82rem;
      color: #fde68a;
      display: flex;
      align-items: flex-start;
      gap: 10px;
      margin-bottom: 20px;
    }

    .security-notice-box .sec-icon {
      font-size: 1.1rem;
      line-height: 1;
    }

    .command-box {
      background: #06040c;
      border: 1px solid rgba(139, 92, 246, 0.3);
      border-radius: 14px;
      padding: 14px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 24px;
    }

    .command-code {
      font-family: var(--font-mono);
      font-size: 0.95rem;
      color: #a7f3d0;
      word-break: break-all;
    }

    .copy-cmd-btn {
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: #fff;
      padding: 7px 12px;
      border-radius: 8px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
      transition: all 0.2s;
    }

    .copy-cmd-btn:hover {
      background: var(--accent-purple);
      border-color: var(--accent-purple);
    }

    .modal-actions {
      display: flex;
      gap: 12px;
    }

    .modal-actions .btn {
      flex: 1;
    }

    /* Toast Notification */
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #1e1b4b;
      border: 1px solid #4338ca;
      color: #e0e7ff;
      padding: 12px 20px;
      border-radius: 12px;
      font-size: 0.88rem;
      font-weight: 600;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
      display: flex;
      align-items: center;
      gap: 10px;
      z-index: 2000;
      opacity: 0;
      transform: translateY(20px);
      transition: all 0.25s ease;
      pointer-events: none;
    }

    .toast.show {
      opacity: 1;
      transform: translateY(0);
    }

    /* Footer */
    footer {
      border-top: 1px solid var(--bg-card-border);
      background: rgba(7, 5, 15, 0.9);
      padding: 40px 20px;
      margin-top: auto;
      text-align: center;
      font-size: 0.84rem;
      color: var(--text-dim);
    }

    .footer-links {
      display: flex;
      justify-content: center;
      gap: 20px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }

    .footer-links a {
      color: var(--text-muted);
      font-weight: 500;
    }

    .footer-links a:hover {
      color: #fff;
    }

    /* Responsive adjustments */
    @media (max-width: 640px) {
      .navbar { padding: 12px 16px; }
      .modules-grid { grid-template-columns: 1fr; }
      .soundpad-grid { grid-template-columns: 1fr; }
      .modal-card { padding: 24px 20px; }
    }
  </style>
</head>
<body>

  <!-- Navigation -->
  <header class="navbar">
    <div class="brand">
      <div class="brand-icon">⚡</div>
      <div class="brand-text">
        <h1>VaPls &amp; El Indio</h1>
        <p>Centro de Control Web &bull; Catálogo de Comandos</p>
      </div>
    </div>
    <div class="nav-actions">
      <a href="/patch-notes" class="nav-link-btn" id="nav-patch-notes">📜 Notas de Parche</a>
      <div class="status-badge" title="Servicios activos en producción">
        <span class="status-dot"></span> Bot Activo &bull; En Línea
      </div>
    </div>
  </header>

  <!-- Main Content -->
  <main class="container">

    <!-- Hero Section -->
    <section class="hero">
      <div class="hero-badge">
        🎙️ Audio de Alta Fidelidad &bull; 📺 Screenshare GoLive &bull; 🧠 IA de Vanguardia
      </div>
      <h2>Consola de Comandos <span class="gradient">VaPls</span></h2>
      <p>
        Explora las capacidades multimedia, transmisión por Go Live, inteligencia artificial del Indio, soundpad y juegos disponibles en tu servidor de Discord.
      </p>

      <!-- Search & Filters -->
      <div class="search-wrapper">
        <span class="search-icon">🔍</span>
        <input type="text" id="command-search" class="search-input" placeholder="Buscar comando o función (ej: play, stream, indio, mascota)..." autocomplete="off" />
        <div class="category-pills">
          <button class="pill-btn active" data-category="all" id="pill-all">Todos</button>
          <button class="pill-btn" data-category="patches" id="pill-patches">📜 Notas de Parche</button>
          <button class="pill-btn" data-category="golive" id="pill-golive">🎬 Stremio &amp; Go Live</button>
          <button class="pill-btn" data-category="music" id="pill-music">🎵 Música</button>
          <button class="pill-btn" data-category="ai" id="pill-ai">🧠 El Indio &amp; IA</button>
          <button class="pill-btn" data-category="soundpad" id="pill-soundpad">🔊 Soundpad</button>
          <button class="pill-btn" data-category="community" id="pill-community">🎮 Utilidades</button>
        </div>
      </div>
    </section>

    <!-- Modules Grid -->
    <section class="modules-grid" id="modules-container">

      <!-- MÓDULO 1: Transmisión Go Live & Stremio -->
      <article class="module-card" data-category="golive" id="card-golive">
        <div class="card-stripe stream"></div>
        <div class="module-header">
          <div class="module-title-box">
            <div class="module-icon">🎬</div>
            <div>
              <h3>Transmisión Go Live &amp; Stremio</h3>
              <p>Películas &amp; Anime por Torrent &bull; IPTV &bull; Twitch (1080p 60fps)</p>
            </div>
          </div>
          <span class="command-tag">/stream</span>
        </div>
        <p class="module-desc">
          Buscador interactivo de <strong>Stremio &amp; Torrents</strong> (TorBox / Torrentio Debrid) y catálogo de <strong>Anime</strong> (Kitsu) para transmitir películas y series en alta definición (1080p/720p 60fps), además de canales de <strong>IPTV mundial</strong> y streams de <strong>Twitch</strong> directamente a tu canal de voz por screenshare Go Live con cifrado DAVE MLS.
        </p>
        <form class="module-form" onsubmit="event.preventDefault(); handleStreamAction();">
          <div class="input-field-group">
            <label class="input-label" for="input-stream-channel">Película, Anime, Torrent o Canal IPTV</label>
            <input type="text" id="input-stream-channel" class="form-input" placeholder="Ej: stremio, Shingeki no Kyojin, Oppenheimer, Telefe..." />
          </div>
          <div class="form-row">
            <button type="button" class="btn btn-secondary" style="flex: 1; border-color: rgba(236, 72, 153, 0.4); color: #f472b6;" onclick="triggerCommand('/stream opcion: stremio', '', 'Genera el enlace web interactivo para buscar películas, series y anime por torrent.')" id="btn-stream-stremio">
              🎬 Abrir Stremio &amp; Anime
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="handleTorrentAction();" id="btn-stream-torrent">
              🧲 Transmitir Torrent
            </button>
          </div>
          <div class="form-row">
            <button type="submit" class="btn btn-primary" style="flex: 2;" id="btn-stream-start">
              ▶ Transmitir en Go Live (/stream)
            </button>
            <button type="button" class="btn btn-danger" style="flex: 1;" onclick="triggerCommand('/stopstream', '', 'Detiene la transmisión Go Live activa en el servidor.')" id="btn-stream-stop">
              ⏹ Detener
            </button>
          </div>
        </form>
        <div class="card-footer">
          <span class="discord-badge">🎬 Stremio &amp; Torrents &bull; ⛩️ Anime</span>
          <span>Soporta IPTV, Twitch y Stremio</span>
        </div>
      </article>

      <!-- MÓDULO 2: Música & Audio -->
      <article class="module-card" data-category="music" id="card-music">
        <div class="card-stripe music"></div>
        <div class="module-header">
          <div class="module-title-box">
            <div class="module-icon">🎵</div>
            <div>
              <h3>Música &amp; Playback</h3>
              <p>Reproducción YouTube, FFmpeg y buffering</p>
            </div>
          </div>
          <span class="command-tag">/play</span>
        </div>
        <p class="module-desc">
          Reproduce cualquier canción, video o playlist de YouTube con colas dinámicas, pre-descarga inteligente y audio de alta fidelidad.
        </p>
        <form class="module-form" onsubmit="event.preventDefault(); handlePlayAction();">
          <div class="input-field-group">
            <label class="input-label" for="input-music-query">Búsqueda o Enlace de YouTube</label>
            <input type="text" id="input-music-query" class="form-input" placeholder="Ej: Soda Stereo - De Música Ligera..." />
          </div>
          <div class="form-row">
            <button type="submit" class="btn btn-primary" style="flex: 2;" id="btn-music-play">
              ▶ Reproducir (/play)
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/parar', '', 'Detiene la reproducción y desconecta al bot del canal de voz.')" id="btn-music-stop">
              ⏹ Parar (/parar)
            </button>
          </div>
          <div class="form-row">
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/quit', '', 'Desconecta al bot del canal de voz sin borrar la lista de reproducción.')" id="btn-music-quit">
              🚪 Salir (/quit)
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/clip duracion: 1m', '', 'Genera y envía un clip de audio con lo ocurrido en el canal de voz.')" id="btn-music-clip">
              🎙️ Grabar Clip (/clip)
            </button>
          </div>
        </form>
        <div class="card-footer">
          <span class="discord-badge">⚡ Pre-buffer y PoT YouTube</span>
          <span>Calidad PCM 48kHz</span>
        </div>
      </article>

      <!-- MÓDULO 3: El Indio & IA -->
      <article class="module-card" data-category="ai" id="card-ai">
        <div class="card-stripe ai"></div>
        <div class="module-header">
          <div class="module-title-box">
            <div class="module-icon">🧠</div>
            <div>
              <h3>El Indio &amp; IA Gemini</h3>
              <p>Personalidad única, memoria y lore</p>
            </div>
          </div>
          <span class="command-tag">/indio</span>
        </div>
        <p class="module-desc">
          Conversa con el Indio (memoria contextual por servidor + memoria destilada a largo plazo) o realiza consultas rápidas y técnicas con VaPls.
        </p>
        <form class="module-form" onsubmit="event.preventDefault(); handleIndioAction();">
          <div class="input-field-group">
            <label class="input-label" for="input-indio-prompt">Mensaje para el Indio</label>
            <input type="text" id="input-indio-prompt" class="form-input" placeholder="Ej: ¿Qué opinas de la pizza con ananá?" />
          </div>
          <div class="form-row">
            <button type="submit" class="btn btn-primary" style="flex: 1;" id="btn-indio-ask">
              💬 Hablar con El Indio
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="handleVaplsAction();" id="btn-vapls-ask">
              ⚡ Consulta Rápida (/vapls)
            </button>
          </div>
          <div class="form-row">
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/entraindio', '', 'Invita al userbot del Indio a ingresar a tu canal de voz actual.')" id="btn-indio-voice">
              🎙️ Entrar a Voz (/entraindio)
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/sensibilidad nivel: 2', '', 'Ajusta la sensibilidad del detector de wake-word del Indio (0 a 4).')" id="btn-indio-sens">
              🎚️ Sensibilidad (/sensibilidad)
            </button>
          </div>
        </form>
        <div class="card-footer">
          <span class="discord-badge">🧠 Gemini 2.5 Flash / Lite</span>
          <span>Voz TTS Piper es_ES-davefx</span>
        </div>
      </article>

      <!-- MÓDULO 4: Soundpad -->
      <article class="module-card" data-category="soundpad" id="card-soundpad">
        <div class="card-stripe soundpad"></div>
        <div class="module-header">
          <div class="module-title-box">
            <div class="module-icon">🔊</div>
            <div>
              <h3>Soundpad Interactivo</h3>
              <p>Clips locales y efectos sonoros</p>
            </div>
          </div>
          <span class="command-tag">/soundpad</span>
        </div>
        <p class="module-desc">
          Dispara efectos de sonido y memes en tiempo real dentro del canal de voz. Explora las carpetas y clips disponibles:
        </p>
        <div class="soundpad-grid">
          <button type="button" class="sound-btn" onclick="triggerCommand('/soundpad', '', 'Abre el panel interactivo de clips y soundpad en tu canal de voz.')">
            <span>🎺</span> Bocina de Aire
          </button>
          <button type="button" class="sound-btn" onclick="triggerCommand('/soundpad', '', 'Abre el panel interactivo de clips y soundpad en tu canal de voz.')">
            <span>👏</span> Aplausos
          </button>
          <button type="button" class="sound-btn" onclick="triggerCommand('/soundpad', '', 'Abre el panel interactivo de clips y soundpad en tu canal de voz.')">
            <span>😂</span> Risa del Indio
          </button>
          <button type="button" class="sound-btn" onclick="triggerCommand('/soundpad', '', 'Abre el panel interactivo de clips y soundpad en tu canal de voz.')">
            <span>🚨</span> Sirena Alerta
          </button>
          <button type="button" class="sound-btn" onclick="triggerCommand('/soundpad', '', 'Abre el panel interactivo de clips y soundpad en tu canal de voz.')">
            <span>💥</span> Golpe Seco
          </button>
          <button type="button" class="sound-btn" onclick="triggerCommand('/soundpad', '', 'Abre el panel interactivo de clips y soundpad en tu canal de voz.')">
            <span>🦆</span> Cuac Pato
          </button>
        </div>
        <button type="button" class="btn btn-secondary" style="width: 100%; margin-top: 4px;" onclick="triggerCommand('/soundpad', '', 'Abre la interfaz completa de botones de audio en Discord.')" id="btn-soundpad-open">
          🎛️ Abrir Menú Completo (/soundpad)
        </button>
        <div class="card-footer">
          <span class="discord-badge">⚡ Playback Instantáneo</span>
          <span>+50 audios categorizados</span>
        </div>
      </article>

      <!-- MÓDULO 5: Minijuegos & Utilidades -->
      <article class="module-card" data-category="community" id="card-community">
        <div class="card-stripe community"></div>
        <div class="module-header">
          <div class="module-title-box">
            <div class="module-icon">🎮</div>
            <div>
              <h3>Comunidad &amp; Utilidades</h3>
              <p>Mascota virtual, trivia y transferencias</p>
            </div>
          </div>
          <span class="command-tag">/mascota</span>
        </div>
        <p class="module-desc">
          Minijuegos interactivos para los miembros del servidor y herramientas seguras de intercambio de archivos pesados.
        </p>
        <div class="module-form">
          <div class="form-row">
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/mascota', '', 'Abre el panel interactivo para alimentar, ver y evolucionar tu mascota virtual.')" id="btn-pet">
              🐶 Mascota Virtual (/mascota)
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/adivinador', '', 'Inicia una partida interactiva de trivia y adivinanza en el canal de texto.')" id="btn-adivinador">
              🔮 Adivinador (/adivinador)
            </button>
          </div>
          <div class="form-row">
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/transferir', '', 'Genera un portal web temporal de alta velocidad para subir y descargar archivos pesados.')" id="btn-transfer">
              📦 Transferir Archivo (/transferir)
            </button>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('/sugerencias idea: Mi nueva idea', '', 'Envía sugerencias de nuevas características clasificadas por IA directamente a los Issues de GitHub.')" id="btn-suggestions">
              💡 Sugerencias (/sugerencias)
            </button>
          </div>
          <button type="button" class="btn btn-secondary" style="width: 100%;" onclick="triggerCommand('/israel-alerts', '', 'Conmuta el feed en vivo de alertas de emergencia en el canal.')" id="btn-israel-alerts">
            🚨 Alertas de Emergencia (/israel-alerts)
          </button>
        </div>
        <div class="card-footer">
          <span class="discord-badge">👾 SQLite &bull; Render ASCII/GIF</span>
          <span>Interacción 100% en Discord</span>
        </div>
      </article>

      <!-- MÓDULO 6: Notas de Parche & Historial -->
      <article class="module-card" data-category="patches" id="card-patch-notes">
        <div class="card-stripe patches"></div>
        <div class="module-header">
          <div class="module-title-box">
            <div class="module-icon">📜</div>
            <div>
              <h3>Notas de Parche &amp; Historial</h3>
              <p>Registro oficial de versiones &bull; Vence en 24h &bull; Archivo permanente</p>
            </div>
          </div>
          <span class="command-tag">/patch-notes</span>
        </div>
        <p class="module-desc">
          Consulta las actualizaciones semanales del bot, balances de IA de El Indio, netcode de Go Live y correcciones técnicas. El enlace directo en Discord vence a las 24 horas por seguridad, pero el registro completo se archiva de forma permanente en la web.
        </p>
        <div class="module-form">
          <div class="form-row">
            <a href="/patch-notes" class="btn btn-primary" style="flex: 2; text-decoration: none;" id="btn-view-patch-history">
              📜 Ver Historial Completo
            </a>
            <button type="button" class="btn btn-secondary" style="flex: 1;" onclick="triggerCommand('https://vapls.duckdns.org/patch-notes', '', 'Explora el registro histórico de cambios y notas de parche archivadas de VaPls.')" id="btn-copy-patch-link">
              🔗 Copiar Enlace
            </button>
          </div>
        </div>
        <div class="card-footer">
          <span class="discord-badge">⏳ 24h Efímero &bull; 📁 Archivo Web</span>
          <span>Actualización semanal automática</span>
        </div>
      </article>

    </section>

  </main>

  <!-- Interactive Modal (The Guardrail) -->
  <div class="modal-overlay" id="command-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-card">
      <button type="button" class="modal-close-btn" onclick="closeCommandModal()" aria-label="Cerrar">&times;</button>
      <div class="modal-icon-header">🔒</div>
      <h3 id="modal-title">Ejecución Exclusiva en Discord</h3>
      <p class="modal-desc" id="modal-desc-text">
        Para usar esta función, ejecuta el siguiente comando slash directamente en tu canal de Discord:
      </p>

      <div class="security-notice-box">
        <span class="sec-icon">🛡️</span>
        <div>
          <strong>Política de Seguridad Web:</strong><br>
          Esta consola pública es de solo lectura y catálogo. Ningún comando ni información confidencial se procesa desde la web para proteger la privacidad de tu servidor.
        </div>
      </div>

      <div class="command-box">
        <span class="command-code" id="modal-command-code">/play query: Soda Stereo</span>
        <button type="button" class="copy-cmd-btn" id="btn-copy-command" onclick="copyCommandToClipboard()">
          📋 Copiar
        </button>
      </div>

      <div class="modal-actions">
        <button type="button" class="btn btn-primary" onclick="copyCommandToClipboard()" id="btn-modal-copy">
          📋 Copiar Comando
        </button>
        <button type="button" class="btn btn-secondary" onclick="closeCommandModal()" id="btn-modal-close">
          Cerrar
        </button>
      </div>
    </div>
  </div>

  <!-- Toast Notification -->
  <div class="toast" id="toast-notify">
    <span>✓</span> <span id="toast-text">¡Comando copiado al portapapeles!</span>
  </div>

  <!-- Footer -->
  <footer>
    <div class="footer-links">
      <a href="/patch-notes">Notas de Parche</a>
      <a href="/privacy">Política de Privacidad</a>
      <a href="/delete-data">Eliminar Datos</a>
      <a href="/stremio">Stremio Web UI</a>
      <a href="https://github.com/dilelu94/VaPls-Discord-Bot" target="_blank" rel="noopener noreferrer">GitHub</a>
    </div>
    <p>
      VaPls Discord Bot &bull; Desarrollado con Py-Cord, CTranslate2, FFmpeg y Gemini &bull; Dominio vapls.duckdns.org
    </p>
    <p style="margin-top: 6px; font-size: 0.78rem; color: #475569;">
      🔒 Entorno seguro: Ningún secreto de API, token o comando interactivo se transmite ni almacena a través de esta página web.
    </p>
  </footer>

  <!-- Interactive Scripts -->
  <script>
    // Modal & Toast Logic
    var activeCommand = "";

    function triggerCommand(command, args, description) {
      var fullCommand = args ? (command + " " + args).trim() : command;
      activeCommand = fullCommand;

      document.getElementById("modal-command-code").innerText = fullCommand;
      if (description) {
        document.getElementById("modal-desc-text").innerText = description + " Para usar esta función, ejecuta el comando en Discord:";
      } else {
        document.getElementById("modal-desc-text").innerText = "Para usar esta función, ejecuta el comando directamente en tu servidor de Discord:";
      }

      var modal = document.getElementById("command-modal");
      modal.classList.add("active");
    }

    function closeCommandModal() {
      var modal = document.getElementById("command-modal");
      modal.classList.remove("active");
    }

    function copyCommandToClipboard() {
      if (!activeCommand) return;
      navigator.clipboard.writeText(activeCommand).then(function() {
        var copyBtn = document.getElementById("btn-copy-command");
        copyBtn.innerText = "✓ ¡Copiado!";
        copyBtn.style.background = "#10b981";
        copyBtn.style.borderColor = "#10b981";

        showToast("¡Comando copiado al portapapeles!");

        setTimeout(function() {
          copyBtn.innerText = "📋 Copiar";
          copyBtn.style.background = "";
          copyBtn.style.borderColor = "";
        }, 2200);
      }).catch(function() {
        showToast("Selecciona el comando y usa Ctrl+C para copiar.");
      });
    }

    function showToast(msg) {
      var toast = document.getElementById("toast-notify");
      var text = document.getElementById("toast-text");
      text.innerText = msg;
      toast.classList.add("show");
      setTimeout(function() {
        toast.classList.remove("show");
      }, 3000);
    }

    function handleStreamAction() {
      var query = document.getElementById("input-stream-channel").value.trim();
      var arg = query ? "opcion: " + query : "opcion: stremio";
      triggerCommand("/stream", arg, "Inicia la transmisión en Go Live (Stremio, Anime, IPTV o Twitch).");
    }

    function handleTorrentAction() {
      var query = document.getElementById("input-stream-channel").value.trim();
      var arg = query ? "opcion: torrent: " + query : "opcion: torrent: Película o Anime";
      triggerCommand("/stream", arg, "Busca y reproduce un torrent de película o anime directamente por screenshare Go Live.");
    }

    function handlePlayAction() {
      var query = document.getElementById("input-music-query").value.trim();
      var arg = query ? "busqueda: " + query : "busqueda: Canción o URL de YouTube";
      triggerCommand("/play", arg, "Reproduce música de YouTube en tu canal de voz.");
    }

    function handleIndioAction() {
      var prompt = document.getElementById("input-indio-prompt").value.trim();
      var arg = prompt ? "mensaje: " + prompt : "mensaje: Hola Indio cómo andas";
      triggerCommand("/indio", arg, "Conversa con el Indio con memoria y personalidad.");
    }

    function handleVaplsAction() {
      var prompt = document.getElementById("input-indio-prompt").value.trim();
      var arg = prompt ? "consulta: " + prompt : "consulta: Pregunta rápida";
      triggerCommand("/vapls", arg, "Consulta técnica directa a Gemini sin memoria conversacional.");
    }

    // Keyboard listener for Escape key to close modal
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") {
        closeCommandModal();
      }
    });

    // Close modal when clicking outside of card
    document.getElementById("command-modal").addEventListener("click", function(e) {
      if (e.target === this) {
        closeCommandModal();
      }
    });

    // Search and Category Filtering
    var searchInput = document.getElementById("command-search");
    var cards = document.querySelectorAll(".module-card");
    var pills = document.querySelectorAll(".pill-btn");

    function filterModules() {
      var query = searchInput.value.toLowerCase().trim();
      var activePill = document.querySelector(".pill-btn.active");
      var category = activePill ? activePill.getAttribute("data-category") : "all";

      cards.forEach(function(card) {
        var cardCat = card.getAttribute("data-category");
        var textContent = card.innerText.toLowerCase();

        var matchesCategory = (category === "all" || cardCat === category);
        var matchesQuery = (!query || textContent.includes(query));

        if (matchesCategory && matchesQuery) {
          card.style.display = "";
        } else {
          card.style.display = "none";
        }
      });
    }

    searchInput.addEventListener("input", filterModules);

    pills.forEach(function(pill) {
      pill.addEventListener("click", function() {
        pills.forEach(function(p) { p.classList.remove("active"); });
        this.classList.add("active");
        filterModules();
      });
    });
  </script>
</body>
</html>"""


# Global singleton instance
landing_page_manager = LandingPageManager()
