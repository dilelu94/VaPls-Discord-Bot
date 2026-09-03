"""Torrent streaming manager for GoLive process.

Launches and manages the local Torrent-to-HTTP engine process (Node.js or Python fallback)
to convert magnet URIs and torrent files into local HTTP video streams for FFmpeg.
"""

import asyncio
import logging
import os
import socket
import sys
from typing import Optional

log = logging.getLogger(__name__)


def find_free_port(start_port: int = 8890, max_attempts: int = 100) -> int:
    """Find an unused TCP port on 127.0.0.1 starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


class TorrentStreamerProcess:
    """Manages a single torrent-to-HTTP engine subprocess."""

    def __init__(self, magnet_or_url: str):
        self.magnet_or_url = magnet_or_url
        self.port = find_free_port()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.stream_url: Optional[str] = None
        self._stopped = False

    async def start(self, timeout: float = 20.0) -> str:
        """Start the torrent streaming engine and return local HTTP stream URL."""
        if not self.magnet_or_url.startswith(("magnet:?", "http://", "https://")):
            if len(self.magnet_or_url) == 40:  # Infohash
                self.magnet_or_url = f"magnet:?xt=urn:btih:{self.magnet_or_url}"

        # If already a direct HTTP(S) video stream (e.g. resolved Debrid URL), return directly
        if self.magnet_or_url.startswith(("http://", "https://")) and not (
            ".torrent" in self.magnet_or_url or "torrentio" in self.magnet_or_url
        ):
            self.stream_url = self.magnet_or_url
            return self.stream_url

        # Path to torrent_engine.js
        script_dir = os.path.dirname(os.path.abspath(__file__))
        js_engine = os.path.join(script_dir, "torrent_engine.js")

        node_bin = "node"
        if not os.path.exists(js_engine):
            log.warning("torrent_engine.js not found at %s", js_engine)
            raise RuntimeError("torrent_engine.js not found")

        log.info("[TORRENT_ENGINE] Spawning node torrent engine on port %d...", self.port)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                node_bin,
                js_engine,
                self.magnet_or_url,
                str(self.port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            log.exception("[TORRENT_ENGINE] Failed to launch node process: %s", e)
            raise RuntimeError(f"Failed to launch torrent engine: {e}")

        # Read stdout line by line waiting for TORRENT_ENGINE_READY
        deadline = asyncio.get_event_loop().time() + timeout
        while not self._stopped:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=min(2.0, remaining))
            except asyncio.TimeoutError:
                if self.proc.returncode is not None:
                    stderr_out = await self.proc.stderr.read()
                    err_txt = stderr_out.decode("utf-8", errors="ignore")
                    log.error("[TORRENT_ENGINE] Process exited with rc=%s: %s", self.proc.returncode, err_txt)
                    raise RuntimeError(f"Torrent engine process failed: {err_txt[:200]}")
                continue

            if not line:
                break

            decoded = line.decode("utf-8", errors="ignore").strip()
            log.info("[TORRENT_ENGINE stdout] %s", decoded)

            if decoded.startswith("TORRENT_ENGINE_READY"):
                parts = decoded.split()
                if len(parts) >= 2:
                    self.stream_url = parts[1]
                    log.info("[TORRENT_ENGINE] Ready! Stream URL: %s", self.stream_url)
                    return self.stream_url

        # Fallback if timeout reached or ready signal missed
        if self.proc and self.proc.returncode is None:
            # Check if port is listening
            self.stream_url = f"http://127.0.0.1:{self.port}/0"
            log.info("[TORRENT_ENGINE] Timeout reached, assuming stream URL %s", self.stream_url)
            return self.stream_url

        raise RuntimeError("Torrent engine failed to initialize stream URL")

    async def stop(self) -> None:
        """Stop the torrent streaming engine and clean up process."""
        if self._stopped:
            return
        self._stopped = True
        if self.proc and self.proc.returncode is None:
            log.info("[TORRENT_ENGINE] Terminating process (pid=%s)...", self.proc.pid)
            try:
                self.proc.terminate()
                await asyncio.sleep(0.2)
                if self.proc.returncode is None:
                    self.proc.kill()
            except Exception as e:
                log.warning("[TORRENT_ENGINE] Error terminating process: %s", e)
        self.proc = None
