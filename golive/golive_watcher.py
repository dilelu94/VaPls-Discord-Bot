"""GoLiveWatcherConnection: Manages a Discord stream watcher connection.

Discreetly sends Opcode 20 (STREAM_WATCH) and Opcode 22 (STREAM_SET_PAUSED: false)
to capture video RTP sample bursts from a target user's Go Live stream without
triggering telemetry flags.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import Optional

import discord
try:
    from discord.gateway import DiscordVoiceWebSocket
except ImportError:
    DiscordVoiceWebSocket = getattr(discord.gateway, "DiscordVoiceWebSocket", None)
try:
    from discord.voice_state import SocketReader
except ImportError:
    SocketReader = None

import davey_compat
from golive.receiver import StreamSnapshotReceiver

log = logging.getLogger(__name__)

_OP_STREAM_WATCH = 20
_OP_STREAM_SET_PAUSED = 22

_rtp_listeners: set = set()


def register_rtp_listener(callback) -> None:
    _rtp_listeners.add(callback)


def unregister_rtp_listener(callback) -> None:
    _rtp_listeners.discard(callback)


def dispatch_rtp_packet(data: bytes) -> None:
    for cb in list(_rtp_listeners):
        try:
            cb(data)
        except Exception:
            pass


class GoLiveWatcherConnection:
    """Manages a discrete stream watcher connection for taking video snapshots."""

    def __init__(
        self,
        bot,
        guild_id: int,
        channel_id: int,
        target_user_id: int,
        vc: discord.VoiceClient,
    ) -> None:
        self._bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.target_user_id = target_user_id
        self._regular_vc = vc

        self.server_id: Optional[int] = None
        self._stream_key: str = f"guild:{guild_id}:{channel_id}:{target_user_id}"

        self.socket: Optional[socket.socket] = None
        self.ws: Optional[DiscordVoiceWebSocket] = None
        self._socket_reader: Optional[SocketReader] = None

        self.dave_session = getattr(vc, "dave_session", None)
        self.receiver = StreamSnapshotReceiver()
        self._connected = False

    @property
    def session_id(self) -> Optional[str]:
        return self._regular_vc.session_id

    @property
    def user(self):
        return self._regular_vc.user

    async def connect(self, timeout: float = 20.0) -> bool:
        """Discreetly sends STREAM_WATCH and STREAM_SET_PAUSED to trigger stream packet flow."""
        main_ws = self._bot.ws
        if not main_ws:
            log.warning("[WATCHER] Main bot websocket is not connected")
            return False

        async def _send_json_safe(ws, data):
            res = ws.send_as_json(data)
            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                await res

        log.info(
            "[WATCHER] Requesting STREAM_WATCH for stream_key=%s",
            self._stream_key,
        )

        try:
            # 1. Send Opcode 20 (STREAM_WATCH)
            await _send_json_safe(
                main_ws,
                {
                    "op": _OP_STREAM_WATCH,
                    "d": {
                        "stream_key": self._stream_key,
                    },
                },
            )

            # 2. Send Opcode 22 (STREAM_SET_PAUSED: false)
            await _send_json_safe(
                main_ws,
                {
                    "op": _OP_STREAM_SET_PAUSED,
                    "d": {
                        "stream_key": self._stream_key,
                        "paused": False,
                    },
                },
            )

            self._connected = True
            log.info("[WATCHER] STREAM_WATCH signal sent successfully")
            return True
        except Exception as exc:
            log.error("[WATCHER] Failed to send STREAM_WATCH: %s", exc)
            return False

    def _on_udp_packet(self, data: bytes) -> None:
        """UDP Socket callback for incoming RTP packets."""
        if not data or len(data) < 12:
            return

        # Check payload type — skip Opus audio packets (typically PT 120 or 111)
        pt = data[1] & 0x7F
        if pt in (120, 111):
            return

        # Extract SSRC from 12-byte RTP header (bytes 8..11)
        ssrc = struct.unpack("!I", data[8:12])[0]

        self.receiver.process_rtp_packet(
            rtp_data=data,
            dave_session=self.dave_session,
            ssrc=ssrc,
            user_id=self.target_user_id,
        )

    async def capture_snapshot(
        self,
        duration_sec: float = 3.0,
        filename: str = "latest_snapshot.jpg",
        max_wait_sec: float = 8.0,
    ) -> Optional[str]:
        """Captures a burst of RTP video packets for duration_sec seconds after waiting for stream handshake."""
        # Bind listener to global RTP dispatcher and VoiceClient's socket reader BEFORE connect
        register_rtp_listener(self._on_udp_packet)
        vc_conn = getattr(self._regular_vc, "_connection", None) or self._regular_vc
        socket_reader = getattr(vc_conn, "_socket_reader", None) or getattr(self._regular_vc, "ws", None)

        if hasattr(vc_conn, "add_socket_listener"):
            vc_conn.add_socket_listener(self._on_udp_packet)
        elif socket_reader and hasattr(socket_reader, "register"):
            socket_reader.register(self._on_udp_packet)

        if not self._connected:
            ok = await self.connect()
            if not ok:
                unregister_rtp_listener(self._on_udp_packet)
                return None

        self.receiver.start_capture()
        log.info("[WATCHER] Waiting for initial stream packets (up to %.1fs)...", max_wait_sec)

        # Wait until video packets start arriving from Discord Gateway (handshake sync)
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < max_wait_sec:
            if len(self.receiver._raw_nal_buffer) > 0:
                log.info("[WATCHER] Stream video packets detected! Collecting sample burst for %.1fs...", duration_sec)
                break
            await asyncio.sleep(0.2)

        # Collect sample burst for duration_sec
        await asyncio.sleep(duration_sec)
        self.receiver.stop_capture()

        # Unregister listener
        unregister_rtp_listener(self._on_udp_packet)
        if hasattr(vc_conn, "remove_socket_listener"):
            vc_conn.remove_socket_listener(self._on_udp_packet)
        elif socket_reader and hasattr(socket_reader, "unregister"):
            socket_reader.unregister(self._on_udp_packet)

        # Extract snapshot JPEG
        jpg_path = self.receiver.extract_snapshot(duration_sec=duration_sec, filename=filename)
        return jpg_path

    async def disconnect(self) -> None:
        """Sends STREAM_SET_PAUSED: true to stop stream packet flow cleanly."""
        if not self._connected:
            return

        main_ws = self._bot.ws
        if main_ws:
            try:
                res = main_ws.send_as_json(
                    {
                        "op": _OP_STREAM_SET_PAUSED,
                        "d": {
                            "stream_key": self._stream_key,
                            "paused": True,
                        },
                    }
                )
                if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                    await res
                log.info("[WATCHER] Sent STREAM_SET_PAUSED: true for %s", self._stream_key)
            except Exception as exc:
                log.warning("[WATCHER] Error disconnecting watcher: %s", exc)

        self._connected = False
