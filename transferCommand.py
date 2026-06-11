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
from typing import Optional

import config

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


class TransferManager:
    def __init__(self):
        self.sessions: dict[str, TransferSession] = {}
        self._lock = asyncio.Lock()
        os.makedirs(config.TRANSFER_DIR, exist_ok=True)
        self._load_index()

    # --- session lifecycle ---------------------------------------------------

    def create_session(
        self, author_id: int, author_name: str, channel_id: int, guild_id: int
    ) -> TransferSession:
        token = uuid.uuid4().hex
        sess = TransferSession(
            token=token,
            author_id=author_id,
            author_name=author_name,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        self.sessions[token] = sess
        self._save_index()
        return sess

    def init_upload(self, token: str, filename: str, total_size: int) -> Optional[str]:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            return "sesión inválida o expirada"
        if total_size > config.TRANSFER_MAX_SIZE:
            return f"el archivo excede el límite de {config.TRANSFER_MAX_SIZE // (1024**3)} GB"
        if not self._check_disk(total_size):
            return "disco lleno, no se puede aceptar el archivo"
        sess.filename = filename
        sess.total_size = total_size
        sess.received = set()
        sess.completed = False
        sess.ready = False
        sess.last_activity = time.time()
        os.makedirs(os.path.join(config.TRANSFER_DIR, token), exist_ok=True)
        self._save_index()
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
            with open(filepath, "ab") as f:
                f.seek(chunk_idx * sess.chunk_size)
                f.write(data)
        except OSError as e:
            return f"error de escritura: {e}"
        sess.received.add(chunk_idx)
        sess.last_activity = time.time()
        return None

    def complete_upload(self, token: str) -> Optional[str]:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            return "sesión inválida o expirada"
        expected = (sess.total_size + sess.chunk_size - 1) // sess.chunk_size
        actual_size = 0
        filepath = os.path.join(config.TRANSFER_DIR, token, sess.filename)
        try:
            actual_size = os.path.getsize(filepath)
        except OSError:
            pass
        if actual_size != sess.total_size:
            return (
                f"tamaño incorrecto: esperado {sess.total_size}, recibido {actual_size}"
            )
        sess.completed = True
        sess.ready = True
        sess.last_activity = time.time()
        self.append_history(sess)
        self._save_index()
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
            remaining = config.TRANSFER_EXPIRY_HOURS * 3600 - age
            if remaining <= 0:
                continue
            out.append(
                {
                    "token": sess.token,
                    "filename": sess.filename,
                    "size": sess.total_size,
                    "author_name": sess.author_name,
                    "remaining_secs": int(remaining),
                    "url": f"{config.TRANSFER_BASE_URL}/dl/{sess.token}/{sess.filename}",
                }
            )
        return out

    def is_session_active(self, token: str) -> bool:
        sess = self.sessions.get(token)
        if not sess or sess.expired:
            return False
        if sess.ready or sess.completed:
            return True
        age = time.time() - sess.last_activity
        return age < config.TRANSFER_SESSION_TTL

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
                if age >= config.TRANSFER_EXPIRY_HOURS * 3600:
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
        if not os.path.isfile(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("failed to load index: %s", e)
            return
        for token, d in data.items():
            d["received"] = set(d.get("received", []))
            sess = TransferSession(**d)
            self.sessions[token] = sess


manager = TransferManager()


UPLOAD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transferir archivos</title>
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
</style>
</head>
<body>
<h1>📁 Transferir archivos</h1>

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
  </div>

  <div id="expired-section" class="card expired" style="display:none">
    <h2>⏰ Sesión expirada</h2>
    <p>Ejecutá <strong>/transferir</strong> en Discord para generar un nuevo link.</p>
  </div>
</div>

<h2>📋 Archivos activos</h2>
<div id="active-files" class="card">
  <p style="color:#8b949e;font-size:0.9rem">Cargando...</p>
</div>

<h2>📜 Historial de archivos subidos</h2>
<div id="history" class="card">
  <p style="color:#8b949e;font-size:0.9rem">Cargando...</p>
</div>

<div id="disk-info" class="disk"></div>

<script>
const TOKEN = "{TOKEN}";
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
    html += `<div class="file-row">
      <div class="file-info">
        <a class="file-name" href="${{f.url}}">${{f.filename}}</a>
        <div class="file-meta">${{sz}} &middot; ${{f.author_name}} &middot; ${{rem}}</div>
      </div>
      <button class="btn btn-danger" onclick="deleteFile('${{f.token}}')">🗑️</button>
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
    const r = await fetch(`/upload/${{fileToken}}`, {{method:"DELETE"}});
    if (r.ok) {{
      loadFiles();
    }} else {{
      alert("Error al borrar");
    }}
  }} catch (e) {{
    alert("Error de red");
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

async function startUpload() {{
  uploading = true;
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
    if (chunksSent.has(i)) {{
      prog.value = Math.round((i / totalChunks) * 100);
      continue;
    }}
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);

    try {{
      const r = await fetch(`/upload/${{TOKEN}}/chunk/${{i}}`, {{method:"POST", body:chunk}});
      if (!r.ok) {{
        const err = await r.json();
        el.innerHTML = '<span class="error">❌ Error en chunk ' + i + ': ' + (err.error || "desconocido") + '</span>';
        uploading = false;
        return;
      }}
    }} catch (e) {{
      el.innerHTML = '<span class="error">❌ Error de red en chunk ' + i + '. Recargá para reanudar.</span>';
      uploading = false;
      return;
    }}
    prog.value = Math.round(((i + 1) / totalChunks) * 100);
  }}

  // complete
  const compR = await fetch(`/upload/${{TOKEN}}/complete`, {{method:"POST"}});
  if (compR.ok) {{
    el.innerHTML = '<span class="success">✅ Archivo subido correctamente</span>';
    document.getElementById("file-input").disabled = true;
    loadFiles();
  }} else {{
    const err = await compR.json();
    el.innerHTML = '<span class="error">❌ Error al finalizar: ' + (err.error || "desconocido") + '</span>';
  }}
  uploading = false;
}}

// --- timer ------------------------------------------------------------------
function updateTimer() {{
  const el = document.getElementById("timer");
  if (!el) return;
  fetch(`/upload/${{TOKEN}}/status`)
    .then(r => r.json())
    .then(d => {{
      if (d.expired) {{
        el.textContent = "⏰ Sesión expirada";
        active = false;
        return;
      }}
      const secs = d.ttl_remaining;
      if (secs <= 0) {{
        el.textContent = "⏰ Sesión expirada";
        active = false;
        checkSession();
        return;
      }}
      const m = Math.floor(secs / 60);
      const s = secs % 60;
      el.textContent = `⏱️ ${{m}}:${{s.toString().padStart(2, "0")}}`;
    }})
    .catch(() => {{}});
}}

async function checkSession() {{
  if (active) return;
  const r = await fetch(`/upload/${{TOKEN}}/status`);
  const d = await r.json();
  if (!d.expired) {{
    active = true;
    document.getElementById("upload-section").style.display = "block";
    document.getElementById("expired-section").style.display = "none";
  }}
}}

// --- init -------------------------------------------------------------------
async function init() {{
  const r = await fetch(`/upload/${{TOKEN}}/status`);
  const d = await r.json();
  if (!d.valid) {{
    document.getElementById("session-state").innerHTML =
      '<div class="card expired"><h2>❌ Token inválido</h2></div>';
    return;
  }}
  if (d.expired) {{
    document.getElementById("upload-section").style.display = "none";
    document.getElementById("expired-section").style.display = "block";
  }} else {{
    if (d.completed) {{
      document.getElementById("file-input").disabled = true;
      document.getElementById("status").innerHTML =
        '<span class="success">✅ Archivo subido</span>';
    }}
    document.getElementById("upload-section").style.display = "block";
  }}
  loadFiles();
  loadHistory();
  setInterval(loadFiles, 10000);
  setInterval(updateTimer, 5000);
  updateTimer();
}}

init();
</script>
</body>
</html>"""


def format_upload_html(token: str) -> str:
    return UPLOAD_HTML.format(
        TOKEN=token,
        DEFAULT_LIMIT=config.TRANSFER_DEFAULT_LIMIT,
        DEFAULT_LIMIT_GB=config.TRANSFER_DEFAULT_LIMIT // (1024**3),
        SESSION_TTL_MIN=config.TRANSFER_SESSION_TTL // 60,
        SESSION_TTL_SECS=config.TRANSFER_SESSION_TTL,
        CHUNK_SIZE=config.TRANSFER_CHUNK_SIZE,
    )
