"""Async log handler que forwardea los logs a un canal/thread de Discord via webhook.

Buffer + flush periódico desde una task asyncio en background. ``emit()`` nunca
bloquea: encola y vuelve. Si la URL no está seteada, el handler es no-op
(podés dejarlo instalado sin riesgo).

Configurable por env vars:

* ``LOG_WEBHOOK_URL`` — URL del webhook (vacío = feature off).
* ``LOG_WEBHOOK_THREAD_ID`` — id del thread destino (opcional).
* ``LOG_WEBHOOK_LEVEL`` — nivel mínimo (default ``INFO``).

Usado por main bot y userbot — cada proceso lo instala con un prefix distinto
así en el thread se distinguen las dos fuentes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
from typing import Optional

import aiohttp

_DISCORD_MSG_LIMIT = 2000
_logger = logging.getLogger("webhookLogger")
_logger.propagate = False  # no realimentar el propio handler


class _DropInternalNoise(logging.Filter):
    """Evita el loop infinito si aiohttp/asyncio logean durante el POST."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        if name == "webhookLogger":
            return False
        if name.startswith("aiohttp.client"):
            return False
        if name.startswith("asyncio"):
            return False
        return True


class DiscordWebhookHandler(logging.Handler):
    """Buffered async log handler que postea a un webhook de Discord.

    ``emit()`` solo encola (thread-safe); el flusher async corre cada
    ``flush_interval`` segundos juntando records y empaquetándolos en mensajes
    de ≤ 2000 chars. Si la queue se llena, descarta los más viejos —
    priorizamos info reciente sobre backfill histórico.
    """

    def __init__(
        self,
        url: str,
        thread_id: Optional[str] = None,
        flush_interval: float = 3.0,
        max_queue: int = 5000,
        level: int = logging.INFO,
    ):
        super().__init__(level=level)
        self.url = (url or "").strip()
        self.thread_id = str(thread_id) if thread_id else None
        self.flush_interval = flush_interval
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_queue)
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.addFilter(_DropInternalNoise())

    def emit(self, record: logging.LogRecord) -> None:
        if not self.url:
            return
        try:
            line = self.format(record)
        except Exception:
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(line)
            except Exception:
                pass

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Arranca la task de flush sobre ``loop``. Idempotente."""
        if not self.url or self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._flusher())

    async def stop(self) -> None:
        """Drenea lo que quede en la queue y para el flusher."""
        if self._stop_event is None or self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except Exception:
            pass

    async def _flusher(self) -> None:
        async with aiohttp.ClientSession() as sess:
            assert self._stop_event is not None
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.flush_interval
                    )
                except asyncio.TimeoutError:
                    pass
                await self._drain(sess)
            # Drain final cuando paramos.
            await self._drain(sess)

    async def _drain(self, sess: aiohttp.ClientSession) -> None:
        chunks: list[str] = []
        while True:
            try:
                chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return
        buf = ""
        for line in chunks:
            if len(line) > _DISCORD_MSG_LIMIT - 10:
                line = line[: _DISCORD_MSG_LIMIT - 10] + "…"
            sep = "\n" if buf else ""
            if len(buf) + len(sep) + len(line) > _DISCORD_MSG_LIMIT:
                await self._post(sess, buf)
                buf = line
            else:
                buf += sep + line
        if buf:
            await self._post(sess, buf)

    async def _post(self, sess: aiohttp.ClientSession, content: str) -> None:
        url = self.url
        if self.thread_id:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}thread_id={self.thread_id}"
        try:
            async with sess.post(
                url,
                json={"content": content},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    # Rate limited — devolvé a la queue para reintento.
                    try:
                        self._queue.put_nowait(content)
                    except queue.Full:
                        pass
                    retry_after = 1.0
                    try:
                        data = await resp.json()
                        retry_after = float(data.get("retry_after", 1.0))
                    except Exception:
                        pass
                    await asyncio.sleep(min(retry_after, 5.0))
                elif resp.status >= 400:
                    body = await resp.text()
                    logging.getLogger("bot.webhook").error(
                        "POST failed %s: %s", resp.status, body[:200]
                    )
        except Exception as e:
            logging.getLogger("bot.webhook").error("POST error: %s", e)


def install_from_env(
    process_name: str,
    env_url: str = "LOG_WEBHOOK_URL",
    env_thread: str = "LOG_WEBHOOK_THREAD_ID",
    env_level: str = "LOG_WEBHOOK_LEVEL",
) -> Optional[DiscordWebhookHandler]:
    """Lee env vars y enchufa un handler al root logger.

    Devuelve el handler (o ``None`` si la URL está vacía). El caller tiene que
    llamar a ``handler.start(loop)`` una vez que el event loop está corriendo
    (típicamente desde ``on_ready``).
    """
    url = os.getenv(env_url, "").strip()
    if not url:
        return None
    thread_id = os.getenv(env_thread, "").strip() or None
    level_name = os.getenv(env_level, "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = DiscordWebhookHandler(url=url, thread_id=thread_id, level=level)
    handler.setFormatter(
        logging.Formatter(
            f"[{process_name}] %(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.getLogger().addHandler(handler)
    return handler
