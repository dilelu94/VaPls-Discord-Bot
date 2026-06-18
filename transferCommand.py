"""File transfer manager for /transferir.

Handles session tokens, chunked upload reassembly, active-file listing,
permanent upload history, and the background sweeper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
import html
from urllib.parse import quote as url_quote
from typing import Optional

import analytics
import config

ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".wmv", ".flv"}
ALLOWED_EXTS = ARCHIVE_EXTS | IMAGE_EXTS | VIDEO_EXTS


def _ext(name: str) -> str:
    _, ext = os.path.splitext(name)
    return ext.lower()


def _is_image(name: str) -> bool:
    return _ext(name) in IMAGE_EXTS


def _is_video(name: str) -> bool:
    return _ext(name) in VIDEO_EXTS


logger = logging.getLogger("transferCommand")


@dataclass
class TransferSession:
    token: str
    author_id: int
    author_name: str
    channel_id: int
    guild_id: int
    filename: str = ""
    total_size: int = 0
    mime_type: str = ""
    received: set[int] = field(default_factory=set)
    chunk_size: int = config.TRANSFER_CHUNK_SIZE
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    completed: bool = False
    ready: bool = False
    expired: bool = False
    completed_at: Optional[float] = None
    delete_token: str = ""
    extended_secs: int = 0
    max_ttl_secs: int = int(config.TRANSFER_EXPIRY_HOURS * 3600)


class TransferManager:
    def __init__(self):
        self.sessions: dict[str, TransferSession] = {}
        self._lock = asyncio.Lock()
        os.makedirs(config.TRANSFER_DIR, exist_ok=True)
        self._load_index()

    # --- session lifecycle ---------------------------------------------------

    def create_session(
        self,
        author_id: int,
        author_name: str,
        channel_id: int,
        guild_id: int,
        days: int = 1,
    ) -> TransferSession:
        token = uuid.uuid4().hex
        max_ttl_secs = min(days, 30) * 86400
        sess = TransferSession(
            token=token,
            author_id=author_id,
            author_name=author_name,
            channel_id=channel_id,
            guild_id=guild_id,
            max_ttl_secs=max_ttl_secs,
        )
        sess.delete_token = uuid.uuid4().hex
        self.sessions[token] = sess
        self._save_index()
        logger.info(
            "session created token=%s author=%s channel=%s",
            token[:8],
            author_name,
            channel_id,
        )
        return sess

    def init_upload(self, token: str, filename: str, total_size: int) -> Optional[str]:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            return "sesión inválida o expirada"
        if sess.filename:
            return "upload ya iniciado"
        if "/" in filename or "\\" in filename:
            logger.warning(
                "upload rejected path traversal token=%s filename=%s",
                token[:8],
                filename,
            )
            analytics.capture(
                "transfer_rejected",
                properties={
                    "reason": "path_traversal",
                    "token": token[:8],
                    "filename": filename,
                },
            )
            return "nombre de archivo inválido"
        ext = _ext(filename)
        if ext not in ALLOWED_EXTS:
            logger.info(
                "upload rejected format token=%s filename=%s ext=%s",
                token[:8],
                filename,
                ext,
            )
            analytics.capture(
                "transfer_rejected",
                properties={
                    "reason": "format_not_allowed",
                    "token": token[:8],
                    "filename": filename,
                    "ext": ext,
                },
            )
            return "formato no permitido — solo archivos comprimidos, imágenes o videos"
        if total_size > config.TRANSFER_MAX_SIZE:
            logger.warning(
                "upload rejected oversize token=%s size=%d max=%d",
                token[:8],
                total_size,
                config.TRANSFER_MAX_SIZE,
            )
            analytics.capture(
                "transfer_rejected",
                properties={
                    "reason": "oversize",
                    "token": token[:8],
                    "size": total_size,
                },
            )
            return f"el archivo excede el límite de {config.TRANSFER_MAX_SIZE // (1024**3)} GB"
        if not self._check_disk(total_size):
            logger.error(
                "upload rejected disk full token=%s size=%d", token[:8], total_size
            )
            analytics.capture(
                "transfer_rejected",
                properties={
                    "reason": "disk_full",
                    "token": token[:8],
                    "size": total_size,
                },
            )
            return "disco lleno, no se puede aceptar el archivo"
        sess.filename = filename
        sess.total_size = total_size
        sess.received = set()
        sess.completed = False
        sess.ready = False
        sess.last_activity = time.time()
        os.makedirs(os.path.join(config.TRANSFER_DIR, token), exist_ok=True)
        self._save_index()
        logger.info(
            "upload init token=%s filename=%s size=%d ext=%s",
            token[:8],
            filename,
            total_size,
            ext,
        )
        analytics.capture(
            "transfer_init",
            properties={
                "token": token[:8],
                "filename": filename,
                "size": total_size,
                "ext": ext,
            },
        )
        return None

    def add_chunk(self, token: str, chunk_idx: int, data: bytes) -> Optional[str]:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            return "sesión inválida o expirada"
        if sess.completed or sess.ready:
            return "upload ya completado"
        dirpath = os.path.join(config.TRANSFER_DIR, token)
        filepath = os.path.join(dirpath, sess.filename)
        try:
            try:
                f = open(filepath, "r+b")
            except FileNotFoundError:
                f = open(filepath, "wb")
            with f:
                f.seek(chunk_idx * sess.chunk_size)
                f.write(data)
        except OSError as e:
            return f"error de escritura: {e}"
        sess.received.add(chunk_idx)
        sess.last_activity = time.time()
        logger.debug(
            "chunk token=%s idx=%d/%d size=%d",
            token[:8],
            chunk_idx + 1,
            (sess.total_size + sess.chunk_size - 1) // sess.chunk_size,
            len(data),
        )
        return None

    def complete_upload(self, token: str) -> Optional[str]:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            logger.warning("complete: invalid/expired session token=%s", token[:8])
            return "sesión inválida o expirada"
        if not sess.filename:
            return "upload no iniciado"
        if sess.total_size <= 0:
            return "tamaño inválido"
        expected = (sess.total_size + sess.chunk_size - 1) // sess.chunk_size
        if len(sess.received) != expected:
            logger.warning(
                "complete: missing chunks token=%s expected=%d received=%d",
                token[:8],
                expected,
                len(sess.received),
            )
            return (
                f"faltan chunks: esperados {expected}, recibidos {len(sess.received)}"
            )
        actual_size = 0
        filepath = os.path.join(config.TRANSFER_DIR, token, sess.filename)
        try:
            actual_size = os.path.getsize(filepath)
        except OSError:
            pass
        if actual_size != sess.total_size:
            logger.warning(
                "complete: size mismatch token=%s expected=%d actual=%d",
                token[:8],
                sess.total_size,
                actual_size,
            )
            return (
                f"tamaño incorrecto: esperado {sess.total_size}, recibido {actual_size}"
            )
        sess.completed = True
        sess.ready = True
        sess.completed_at = time.time()
        sess.last_activity = time.time()
        self.append_history(sess)
        self._save_index()
        logger.info(
            "upload complete token=%s filename=%s size=%d",
            token[:8],
            sess.filename,
            sess.total_size,
        )
        return None

    def delete(self, token: str) -> bool:
        sess = self.sessions.get(token)
        if not sess:
            return False
        dirpath = os.path.join(config.TRANSFER_DIR, token)
        if os.path.isdir(dirpath):
            shutil.rmtree(dirpath, ignore_errors=True)
        sess.expired = True
        sess.ready = False
        self._save_index()
        return True

    def get(self, token: str) -> Optional[TransferSession]:
        return self.sessions.get(token)

    def extend(self, token: str) -> None:
        sess = self.sessions.get(token)
        if sess:
            extra = config.TRANSFER_EXPIRY_HOURS * 3600
            max_extra = sess.max_ttl_secs - extra
            sess.extended_secs = min(
                sess.extended_secs + int(extra),
                max(int(max_extra), 0),
            )
            self._save_index()

    def touch(self, token: str) -> None:
        sess = self.sessions.get(token)
        if sess:
            sess.last_activity = time.time()

    # --- queries ------------------------------------------------------------

    def get_active_files(self) -> list[dict]:
        now = time.time()
        out = []
        for sess in self.sessions.values():
            if sess.expired or not sess.ready:
                continue
            age = now - sess.last_activity
            total_ttl = min(
                int(config.TRANSFER_EXPIRY_HOURS * 3600) + sess.extended_secs,
                sess.max_ttl_secs,
            )
            remaining = total_ttl - age
            if remaining <= 0:
                continue
            out.append(
                {
                    "token": sess.token,
                    "filename": sess.filename,
                    "size": sess.total_size,
                    "author_name": sess.author_name,
                    "remaining_secs": int(remaining),
                    "url": f"{config.TRANSFER_BASE_URL}/dl/{sess.token}/{url_quote(sess.filename)}",
                }
            )
        return out

    def is_upload_expired(self, token: str) -> bool:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            return True
        if not sess.ready:
            age = time.time() - sess.last_activity
            return age >= config.TRANSFER_SESSION_TTL
        if sess.completed_at is None:
            return True
        age = time.time() - sess.completed_at
        return age >= config.TRANSFER_SESSION_TTL

    # --- history ------------------------------------------------------------

    def append_history(self, sess: TransferSession) -> None:
        entry = {
            "author_name": sess.author_name,
            "filename": sess.filename,
            "size": sess.total_size,
            "uploaded_at": int(time.time()),
            "token": sess.token,
        }
        try:
            with open(config.TRANSFER_HISTORY_PATH, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("failed to write history: %s", e)

    def get_history(self, limit: int = 100) -> list[dict]:
        if not os.path.isfile(config.TRANSFER_HISTORY_PATH):
            return []
        entries = []
        try:
            with open(config.TRANSFER_HISTORY_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError as e:
            logger.warning("failed to read history: %s", e)
            return []
        entries.reverse()
        return entries[:limit]

    # --- sweeper ------------------------------------------------------------

    async def sweep(self) -> None:
        now = time.time()
        to_delete = []
        for token, sess in self.sessions.items():
            if sess.expired:
                continue
            if not sess.completed and not sess.ready:
                age = now - sess.last_activity
                if age >= config.TRANSFER_SESSION_TTL:
                    to_delete.append(token)
                    continue
            if sess.ready:
                age = now - sess.last_activity
                total_ttl = min(
                    int(config.TRANSFER_EXPIRY_HOURS * 3600) + sess.extended_secs,
                    sess.max_ttl_secs,
                )
                if age >= total_ttl:
                    to_delete.append(token)
        for token in to_delete:
            self.delete(token)
            logger.info("sweep: deleted expired session %s", token)
        if to_delete:
            self._save_index()

    async def start_sweeper(self) -> None:
        while True:
            await asyncio.sleep(config.TRANSFER_SWEEPER_INTERVAL)
            try:
                await self.sweep()
            except Exception:
                logger.exception("sweeper error")

    # --- internals ----------------------------------------------------------

    def _check_disk(self, needed: int) -> bool:
        try:
            st = os.statvfs(config.TRANSFER_DIR)
            free = st.f_frsize * st.f_bavail
            return free >= needed + config.TRANSFER_DISK_RESERVE
        except OSError:
            return True

    def _save_index(self) -> None:
        path = os.path.join(config.TRANSFER_DIR, "_index.json")
        data = {}
        for token, sess in self.sessions.items():
            d = asdict(sess)
            d["received"] = list(sess.received)
            data[token] = d
        try:
            with open(path, "w") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as e:
            logger.warning("failed to save index: %s", e)

    def _load_index(self) -> None:
        path = os.path.join(config.TRANSFER_DIR, "_index.json")
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("failed to load index: %s", e)
                data = {}
            for token, d in data.items():
                d["received"] = set(d.get("received", []))
                sess = TransferSession(**d)
                self.sessions[token] = sess
        # Scan disk for directory tokens not in index (survives deploy resets)
        try:
            for entry in os.listdir(config.TRANSFER_DIR):
                dirpath = os.path.join(config.TRANSFER_DIR, entry)
                if not os.path.isdir(dirpath) or entry.startswith("_"):
                    continue
                if entry in self.sessions:
                    continue
                files = os.listdir(dirpath)
                if not files:
                    continue
                filename = files[0]
                filepath = os.path.join(dirpath, filename)
                fsize = os.path.getsize(filepath)
                author_name = "Desconocido"
                author_id = 0
                channel_id = 0
                guild_id = 0
                try:
                    with open(config.TRANSFER_HISTORY_PATH) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                h = json.loads(line)
                                if h.get("token") == entry:
                                    author_name = h.get("author_name", "Desconocido")
                                    break
                            except json.JSONDecodeError:
                                continue
                except (OSError, FileNotFoundError):
                    pass
                sess = TransferSession(
                    token=entry,
                    author_id=author_id,
                    author_name=author_name,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    filename=filename,
                    total_size=fsize,
                    ready=True,
                    completed=True,
                    completed_at=os.path.getmtime(filepath),
                    last_activity=os.path.getmtime(filepath),
                    created_at=os.path.getctime(dirpath),
                    delete_token="",
                )
                self.sessions[entry] = sess
                logger.info(
                    "recovered session from disk token=%s filename=%s",
                    entry[:8],
                    filename,
                )
        except OSError:
            pass


manager = TransferManager()


DOWNLOAD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Descargar archivo</title>
<link rel="icon" href="/static/icon.jpg" type="image/jpeg">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 600px; margin: auto; display: flex; min-height: 100vh; align-items: center; justify-content: center; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 32px; text-align: center; width: 100%; }}
  .icon {{ width: 96px; height: 96px; border-radius: 12px; object-fit: cover; margin-bottom: 8px; }}
  .filename {{ font-size: 1.1rem; color: #58a6ff; margin: 12px 0 4px; word-break: break-all; }}
  .size {{ font-size: 0.85rem; color: #8b949e; margin-bottom: 24px; }}
  .media {{ max-width: 100%; max-height: 70vh; border-radius: 8px; margin-bottom: 16px; }}
  .btn {{ display: inline-block; padding: 12px 32px; border-radius: 6px; border: none; cursor: pointer; font-size: 16px; text-decoration: none; }}
  .btn-download {{ background: #238636; color: #fff; }}
  .btn-download:hover {{ background: #2ea043; }}
  .gone {{ color: #8b949e; font-size: 1rem; margin-top: 12px; }}
</style>
</head>
<body>
<div class="card">
  <div id="available">
    <img class="icon" src="/static/icon.jpg" alt="icon">
    <div class="filename">{FILENAME}</div>
    <div class="size">{SIZE}</div>
    <div id="media-preview" style="display:none;margin-bottom:16px"></div>
    <a class="btn btn-download" href="{RAW_URL}" id="dl-btn">⬇️ Descargar</a>
  </div>
  <div id="unavailable" style="display:none">
    <div style="font-size:3rem;margin-bottom:8px">❌</div>
    <div class="gone">Archivo no disponible</div>
  </div>
</div>
<script>
var ok = {OK};
var mt = "{MEDIA_TYPE}";
var raw = "{RAW_URL}";
if (!ok) {{
  document.getElementById("available").style.display = "none";
  document.getElementById("unavailable").style.display = "block";
}}
if (mt === "image") {{
  document.getElementById("dl-btn").style.display = "none";
  document.getElementById("media-preview").style.display = "block";
  document.getElementById("media-preview").innerHTML = '<img class="media" src="' + raw + '" alt="' + document.querySelector(".filename").textContent + '">';
}} else if (mt === "video") {{
  document.getElementById("dl-btn").style.display = "none";
  document.getElementById("media-preview").style.display = "block";
  document.getElementById("media-preview").innerHTML = '<video class="media" src="' + raw + '" controls autoplay loop></video>';
}}
</script>
</body>
</html>"""

UPLOAD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transferir archivos</title>
<link rel="icon" href="/static/icon.jpg" type="image/jpeg">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 800px; margin: auto; }}
  h1 {{ color: #58a6ff; margin-bottom: 8px; }}
  h2 {{ color: #8b949e; font-size: 1.1rem; margin: 20px 0 8px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .btn {{ display: inline-block; padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 14px; }}
  .btn-primary {{ background: #238636; color: #fff; }}
  .btn-primary:hover {{ background: #2ea043; }}
  .btn-primary:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .btn-danger {{ background: #da3633; color: #fff; }}
  .btn-danger:hover {{ background: #f85149; }}
  .btn-extend {{ background: #1f6feb; color: #fff; padding: 6px 10px; border-radius: 6px; border: none; cursor: pointer; font-size: 14px; }}
  .btn-extend:hover {{ background: #388bfd; }}
  progress {{ width: 100%; height: 12px; border-radius: 6px; margin: 8px 0; }}
  progress::-webkit-progress-bar {{ background: #21262d; border-radius: 6px; }}
  progress::-webkit-progress-value {{ background: #238636; border-radius: 6px; }}
  .timer {{ font-size: 0.9rem; color: #f0883e; }}
  .file-row {{ display: flex; align-items: center; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #21262d; }}
  .file-row:last-child {{ border-bottom: none; }}
  .file-info {{ flex: 1; }}
  .file-name {{ color: #58a6ff; text-decoration: none; font-weight: 500; }}
  .file-name:hover {{ text-decoration: underline; }}
  .file-meta {{ font-size: 0.8rem; color: #8b949e; margin-top: 2px; }}
  .expired {{ color: #da3633; text-align: center; padding: 40px 0; }}
  .expired h2 {{ color: #da3633; font-size: 1.5rem; }}
  .hist-row {{ display: flex; align-items: center; padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 0.9rem; }}
  .hist-row:last-child {{ border-bottom: none; }}
  .hist-user {{ color: #58a6ff; min-width: 120px; }}
  .hist-file {{ flex: 1; }}
  .hist-date {{ color: #8b949e; min-width: 100px; text-align: right; }}
  #status {{ margin: 8px 0; }}
  .error {{ color: #da3633; }}
  .success {{ color: #3fb950; }}
  .disk {{ font-size: 0.8rem; color: #8b949e; text-align: right; margin-top: 12px; }}
  .disclaimer {{ background: #da3633; color: #fff; padding: 10px 16px; border-radius: 8px; margin-bottom: 12px; font-size: 0.85rem; text-align: center; font-weight: 500; }}
</style>
</head>
<body>
<h1>📁 Transferir archivos</h1>

<div class="disclaimer">⚠️ Todo archivo subido queda registrado permanentemente con nombre de usuario e ID de Discord</div>

<div id="session-state">
  <div id="upload-section" class="card" style="display:none">
    <h2>📤 Subir archivo</h2>
    <p style="font-size:0.85rem;color:#8b949e;margin-bottom:8px">
      Max <strong>{DEFAULT_LIMIT_GB} GB</strong> por archivo &middot;
      El link de sesión vence en <strong>{SESSION_TTL_MIN} min</strong> si no hay actividad
    </p>
    <input type="file" id="file-input" style="margin-bottom:8px;width:100%">
    <progress id="progress" value="0" max="100"></progress>
    <div id="status"></div>
    <div id="timer" class="timer"></div>
    <button class="btn btn-danger" id="cancel-btn" style="display:none;margin-top:8px" onclick="cancelUpload()">✕ Cancelar</button>
  </div>

  <div id="completed-section" class="card" style="display:none;text-align:center">
    <h2 style="color:#3fb950">✅ Archivo subido</h2>
    <p id="completed-filename" style="font-size:1.1rem;margin:8px 0"></p>
    <input type="text" id="completed-link" readonly
      style="width:100%;padding:8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#58a6ff;text-align:center;margin:8px 0;font-size:0.9rem"
      onclick="this.select();copyLink()">
    <button class="btn btn-primary" onclick="copyLink()" style="margin-top:4px">📋 Copiar link</button>
  </div>

  <div id="expired-section" class="card expired" style="display:none">
    <h2>⏰ Sesión expirada</h2>
    <p>Ejecutá <strong>/transferir</strong> en Discord para generar un nuevo link.</p>
    <p style="margin-top:6px">Volvé a usar <strong>/transferir</strong></p>
    <div id="expired-file-info" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid #30363d">
      <p id="expired-filename" style="font-size:0.9rem;margin-bottom:8px"></p>
      <button class="btn btn-danger" onclick="deleteExpired()">🗑️ Borrar archivo</button>
      <p id="deleted-msg" style="color:#3fb950;font-size:0.9rem;display:none;margin-top:8px">✅ Archivo borrado</p>
    </div>
  </div>
</div>

<div id="extra-sections">
  <h2 id="files-heading">📋 Archivos activos</h2>
  <div id="active-files" class="card">
    <p style="color:#8b949e;font-size:0.9rem">Cargando...</p>
  </div>

  <h2 id="history-heading">📜 Historial de archivos subidos</h2>
  <div id="history" class="card">
    <p style="color:#8b949e;font-size:0.9rem">Cargando...</p>
  </div>

  <div id="disk-info" class="disk"></div>
</div>

<script>
const TOKEN = "{TOKEN}";
const DELETE_TOKEN = "{DELETE_TOKEN}";
const DEFAULT_LIMIT = {DEFAULT_LIMIT};
const SESSION_TTL_SECS = {SESSION_TTL_SECS};
const CHUNK_SIZE = {CHUNK_SIZE};

let active = true;
let file = null;
let chunksSent = new Set();
let totalChunks = 0;
let uploading = false;

async function loadFiles() {{
  try {{
    const r = await fetch(`/upload/${{TOKEN}}/files`);
    const data = await r.json();
    renderFiles(data.files || []);
    renderDisk(data.disk_free, data.disk_total);
  }} catch (e) {{
    document.getElementById("active-files").innerHTML =
      '<p style="color:#da3633">Error al cargar archivos</p>';
  }}
}}

async function loadHistory() {{
  try {{
    const r = await fetch(`/upload/${{TOKEN}}/history`);
    const data = await r.json();
    renderHistory(data.history || []);
  }} catch (e) {{
    document.getElementById("history").innerHTML =
      '<p style="color:#da3633">Error al cargar historial</p>';
  }}
}}

function esc(s) {{
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}}

function renderFiles(files) {{
  const el = document.getElementById("active-files");
  if (!files.length) {{
    el.innerHTML = '<p style="color:#8b949e;font-size:0.9rem">Sin archivos activos</p>';
    return;
  }}
  let html = "";
  for (const f of files) {{
    const rem = formatTime(f.remaining_secs);
    const sz = formatSize(f.size);
    const extBtn = `<button class="btn btn-extend" onclick="extendFile('${{f.token}}')" title="Extender 24h">➕</button>`;
    html += `<div class="file-row">
      <div class="file-info">
        <a class="file-name" href="${{f.url}}">${{esc(f.filename)}}</a>
        <div class="file-meta">${{sz}} &middot; ${{esc(f.author_name)}} &middot; ${{rem}}</div>
      </div>
      <div style="display:flex;gap:4px">
        ${{extBtn}}
        <button class="btn btn-danger" onclick="deleteFile('${{f.token}}')">🗑️</button>
      </div>
    </div>`;
  }}
  el.innerHTML = html;
}}

function renderHistory(entries) {{
  const el = document.getElementById("history");
  if (!entries.length) {{
    el.innerHTML = '<p style="color:#8b949e;font-size:0.9rem">Sin historial</p>';
    return;
  }}
  let html = "";
  for (const e of entries) {{
    const d = new Date(e.uploaded_at * 1000);
    const dateStr = d.toLocaleDateString("es-ES", {{day:"2-digit",month:"2-digit"}});
    const timeStr = d.toLocaleTimeString("es-ES", {{hour:"2-digit",minute:"2-digit"}});
    const sz = formatSize(e.size);
    html += `<div class="hist-row">
      <span class="hist-user">${{e.author_name}}</span>
      <span class="hist-file">${{e.filename}} <span style="color:#8b949e">(${{sz}})</span></span>
      <span class="hist-date">${{dateStr}} ${{timeStr}}</span>
    </div>`;
  }}
  el.innerHTML = html;
}}

function renderDisk(free, total) {{
  const used = total - free;
  const pct = ((used / total) * 100).toFixed(1);
  document.getElementById("disk-info").textContent =
    `💾 ${{formatSize(used)}} / ${{formatSize(total)}} usado (${{pct}}%)`;
}}

function formatSize(bytes) {{
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + " GB";
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(0) + " KB";
  return bytes + " B";
}}

function formatTime(secs) {{
  if (secs <= 0) return "expirado";
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return h + "h " + m + "m ⏳";
  return m + "m ⏳";
}}

async function deleteFile(fileToken) {{
  if (!confirm("¿Borrar este archivo?")) return;
  try {{
    const r = await fetch(`/upload/${{fileToken}}?dt=${{DELETE_TOKEN}}`, {{method:"DELETE"}});
    if (r.ok) {{
      loadFiles();
    }} else {{
      alert("Error al borrar");
    }}
  }} catch (e) {{
    alert("Error de red");
  }}
}}

async function extendFile(fileToken) {{
  try {{
    const r = await fetch(`/upload/${{fileToken}}/extend`, {{method:"POST"}});
    if (r.ok) {{
      const data = await r.json();
      loadFiles();
    }}
  }} catch (e) {{
    console.error("extend error", e);
  }}
}}

// --- chunked upload ---------------------------------------------------------
document.getElementById("file-input").addEventListener("change", function(e) {{
  file = e.target.files[0];
  if (!file) return;
  if (file.size > DEFAULT_LIMIT) {{
    document.getElementById("status").innerHTML =
      '<span class="error">❌ El archivo excede el límite de ' + (DEFAULT_LIMIT / (1024*1024*1024)) + ' GB</span>';
    file = null;
    return;
  }}
  startUpload();
}});

let uploadStartTime = null;
let uploadedBytes = 0;

function updateUploadETA() {{
  const el = document.getElementById("timer");
  if (!el || !uploading || !uploadStartTime) return;
  const elapsed = (Date.now() - uploadStartTime) / 1000;
  if (elapsed < 2 || uploadedBytes < 1) return;
  const speed = uploadedBytes / elapsed;
  const remaining = file.size - uploadedBytes;
  const eta = remaining / speed;
  if (eta <= 0 || !isFinite(eta)) return;
  const m = Math.floor(eta / 60);
  const s = Math.floor(eta % 60);
  el.textContent = `⏱️ Tiempo estimado: ${{m}}:${{s.toString().padStart(2, "0")}}`;
}}

async function startUpload() {{
  uploading = true;
  uploadStartTime = Date.now();
  uploadedBytes = 0;
  document.getElementById("cancel-btn").style.display = "inline-block";
  const el = document.getElementById("status");
  const prog = document.getElementById("progress");
  prog.value = 0;

  // init
  const initR = await fetch(`/upload/${{TOKEN}}/init`, {{
    method: "POST",
    headers: {{"Content-Type":"application/json"}},
    body: JSON.stringify({{filename:file.name, size:file.size}})
  }});
  if (!initR.ok) {{
    const err = await initR.json();
    el.innerHTML = '<span class="error">❌ ' + (err.error || "error al iniciar") + '</span>';
    uploading = false;
    return;
  }}

  // check resumable status
  const statusR = await fetch(`/upload/${{TOKEN}}/status`);
  const statusData = await statusR.json();
  chunksSent = new Set(statusData.received || []);
  totalChunks = Math.ceil(file.size / CHUNK_SIZE);

  for (let i = 0; i < totalChunks; i++) {{
    if (!uploading) return;
    if (chunksSent.has(i)) {{
      uploadedBytes += Math.min(CHUNK_SIZE, file.size - uploadedBytes);
      prog.value = Math.round((i / totalChunks) * 100);
      continue;
    }}
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);

    const maxRetries = 3;
    for (let attempt = 0; attempt < maxRetries; attempt++) {{
      if (attempt > 0) await new Promise(r => setTimeout(r, 1000));
      try {{
        const r = await fetch(`/upload/${{TOKEN}}/chunk/${{i}}`, {{method:"POST", body:chunk}});
        if (r.ok) {{ uploadedBytes += chunk.size; break; }}
        if (attempt === maxRetries - 1) {{
          const err = await r.json();
          el.innerHTML = '<span class="error">❌ Error en chunk ' + i + ': ' + (err.error || "desconocido") + '</span>';
          uploading = false;
          return;
        }}
      }} catch (e) {{
        if (attempt === maxRetries - 1) {{
          el.innerHTML = '<span class="error">❌ Error de red en chunk ' + i + ' tras ' + maxRetries + ' intentos.</span>';
          uploading = false;
          return;
        }}
      }}
    }}
    prog.value = Math.round(((i + 1) / totalChunks) * 100);
    updateUploadETA();
  }}

  // complete
  const compR = await fetch(`/upload/${{TOKEN}}/complete`, {{method:"POST"}});
  if (compR.ok) {{
    uploading = false;
    document.getElementById("cancel-btn").style.display = "none";
    document.getElementById("upload-section").style.display = "none";
    document.getElementById("completed-filename").textContent = file.name;
    document.getElementById("completed-link").value = window.location.origin + "/dl/" + TOKEN + "/" + encodeURIComponent(file.name);
    document.getElementById("completed-section").style.display = "block";
    document.getElementById("status").innerHTML = '<span class="success">✅ Archivo subido correctamente</span>';
    loadFiles();
    loadHistory();
    return;
  }} else {{
    const err = await compR.json();
    el.innerHTML = '<span class="error">❌ Error al finalizar: ' + (err.error || "desconocido") + '</span>';
  }}
  document.getElementById("cancel-btn").style.display = "none";
  uploading = false;
}}

async function cancelUpload() {{
  if (!confirm("¿Cancelar la subida?")) return;
  uploading = false;
  document.getElementById("cancel-btn").style.display = "none";
  document.getElementById("status").innerHTML = '<span style="color:#8b949e">✖ Subida cancelada</span>';
  try {{
    await fetch(`/upload/${{TOKEN}}?dt=${{DELETE_TOKEN}}`, {{method:"DELETE"}});
  }} catch (e) {{}}
}}

// --- timer ------------------------------------------------------------------
var localTtl = 0;
var localTtlAt = 0;
var tickId = null;

function showExpired(el) {{
  el.textContent = "⏰ Sesión expirada";
  active = false;
  document.getElementById("upload-section").style.display = "none";
  document.getElementById("expired-section").style.display = "block";
  if (tickId) clearInterval(tickId);
  tickId = null;
}}

function showSecs(el, secs) {{
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  el.textContent = `⏱️ Sesión activa: ${{m}}:${{s.toString().padStart(2, "0")}}`;
}}

function syncTimer() {{
  const el = document.getElementById("timer");
  if (!el || uploading) return;

  fetch(`/upload/${{TOKEN}}/status`)
    .then(r => r.json())
    .then(d => {{
      if (d.expired) {{ showExpired(el); return; }}
      if (d.completed) {{
        if (d.file_exists) {{
          window.location.href = "/dl/" + TOKEN + "/" + encodeURIComponent(d.filename || "");
        }}
        return;
      }}
      if (d.ttl_remaining <= 0) {{ showExpired(el); return; }}
      localTtl = d.ttl_remaining;
      localTtlAt = Date.now();
      if (!tickId) {{
        tickId = setInterval(tick, 1000);
        tick();
      }}
    }})
    .catch(() => {{}});
}}

function tick() {{
  const el = document.getElementById("timer");
  if (!el || uploading) return;
  const elapsed = (Date.now() - localTtlAt) / 1000;
  const secs = localTtl - elapsed;
  if (secs <= 0) {{ showExpired(el); return; }}
  showSecs(el, secs);
}}

function copyLink() {{
  const el = document.getElementById("completed-link");
  if (!el) return;
  el.select();
  try {{
    document.execCommand("copy");
  }} catch (e) {{
    // execCommand fallback — works even on HTTP
  }}
}}

async function deleteExpired() {{
  try {{
    const r = await fetch(`/upload/${{TOKEN}}?dt=${{DELETE_TOKEN}}`, {{method:"DELETE"}});
    if (r.ok) {{
      document.getElementById("expired-file-info").style.display = "none";
      document.getElementById("deleted-msg").style.display = "block";
    }} else {{
      const body = await r.json();
      alert(body.error || "Error al borrar");
    }}
  }} catch (e) {{
    alert("Error de red");
  }}
}}

// --- init -------------------------------------------------------------------
async function init() {{
  const r = await fetch(`/upload/${{TOKEN}}/status`);
  const d = await r.json();
  if (!d.valid) {{
    document.getElementById("session-state").innerHTML =
      '<div class="card expired"><h2>❌ Token inválido</h2></div>';
    document.getElementById("extra-sections").style.display = "none";
    return;
  }}
  if (d.expired || d.completed) {{
    document.getElementById("session-state").innerHTML =
      '<div class="card" style="text-align:center;padding:40px"><p style="color:#8b949e;font-size:0.9rem">' +
      (d.completed ? '✅ Archivo subido' : '⏰ Sesión expirada') +
      '</p></div>';
    document.getElementById("extra-sections").style.display = "none";
    if (d.completed && d.filename) {{
      window.location.href = "/dl/" + TOKEN + "/" + encodeURIComponent(d.filename);
    }}
    return;
  }}
  document.getElementById("upload-section").style.display = "block";
  loadFiles();
  loadHistory();
  setInterval(loadFiles, 10000);
  setInterval(syncTimer, 5000);
  syncTimer();
}}

init();
</script>
</body>
</html>"""


def format_download_html(token: str, filename: str, size: int, ok: bool) -> str:
    if ok:
        gb_val = size / (1024**3)
        sz = f"{gb_val:.1f} GB" if gb_val >= 1 else f"{size / (1024**2):.0f} MB"
    else:
        sz = ""
    mt = "image" if _is_image(filename) else "video" if _is_video(filename) else ""
    return DOWNLOAD_HTML.format(
        FILENAME=html.escape(filename),
        SIZE=sz,
        RAW_URL=f"/dl/{token}/{url_quote(filename)}/raw",
        OK="true" if ok else "false",
        MEDIA_TYPE=mt,
    )


def format_upload_html(token: str, delete_token: str = "") -> str:
    return UPLOAD_HTML.format(
        TOKEN=token,
        DELETE_TOKEN=delete_token,
        DEFAULT_LIMIT=config.TRANSFER_DEFAULT_LIMIT,
        DEFAULT_LIMIT_GB=config.TRANSFER_DEFAULT_LIMIT // (1024**3),
        SESSION_TTL_MIN=config.TRANSFER_SESSION_TTL // 60,
        SESSION_TTL_SECS=config.TRANSFER_SESSION_TTL,
        CHUNK_SIZE=config.TRANSFER_CHUNK_SIZE,
    )
