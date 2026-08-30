"""GoLiveWatcherConnection: Manages a Discord stream watcher connection.

Discreetly sends Opcode 20 (STREAM_WATCH) and Opcode 22 (STREAM_SET_PAUSED: false)
to capture video RTP sample bursts from a target user's Go Live stream without
triggering telemetry flags.
"""

from __future__ import annotations

import asyncio
import inspect
import json
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


async def _send_gateway_json(ws, data: dict) -> bool:
    """Safely sends a JSON opcode dictionary over any Discord Gateway WebSocket type."""
    if ws is None:
        return False
    payload_str = json.dumps(data)

    # Check for send_as_json first
    send_as_json_fn = getattr(ws, "send_as_json", None)
    if send_as_json_fn is not None and callable(send_as_json_fn):
        try:
            res = send_as_json_fn(data)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception as exc:
            log.debug("[WATCHER] send_as_json failed: %s", exc)

    # Check for send method
    send_fn = getattr(ws, "send", None)
    if send_fn is not None and callable(send_fn):
        try:
            res = send_fn(payload_str)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception as exc:
            log.debug("[WATCHER] send failed: %s", exc)

    # Check for send_str method (aiohttp)
    send_str_fn = getattr(ws, "send_str", None)
    if send_str_fn is not None and callable(send_str_fn):
        try:
            res = send_str_fn(payload_str)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception:
            pass

    log.error("[WATCHER] Unable to send JSON payload over websocket %s", type(ws))
    return False


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

        state = getattr(vc, "_connection", None)
        self.dave_session = getattr(state, "dave_session", None) if state else getattr(vc, "dave_session", None)
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

        log.info(
            "[WATCHER] Requesting STREAM_WATCH for stream_key=%s",
            self._stream_key,
        )

        try:
            # 1. Send Opcode 20 (STREAM_WATCH)
            ok1 = await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_WATCH,
                    "d": {
                        "stream_key": self._stream_key,
                        "video_codec": "H264",
                    },
                },
            )

            # 2. Send Opcode 22 (STREAM_SET_PAUSED: false)
            ok2 = await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_SET_PAUSED,
                    "d": {
                        "stream_key": self._stream_key,
                        "paused": False,
                    },
                },
            )

            self._connected = ok1 or ok2
            log.info("[WATCHER] STREAM_WATCH signal sent (status=%s)", self._connected)
            return self._connected
        except Exception as exc:
            log.error("[WATCHER] Failed to send STREAM_WATCH: %s", exc, exc_info=True)
            return False

    def _on_udp_packet(self, data: bytes) -> None:
        """UDP Socket callback for incoming RTP packets."""
        if not data or len(data) < 12:
            return

        # 0. Apply Discord RTP transport decryption if key/mode is present
        vc = self._regular_vc
        mode = getattr(vc, "mode", None) or getattr(getattr(vc, "_connection", None), "mode", None)
        secret_key = getattr(vc, "secret_key", None) or getattr(getattr(vc, "_connection", None), "secret_key", None)
        if secret_key and isinstance(secret_key, (list, tuple)):
            secret_key = bytes(secret_key)

        if mode and secret_key:
            if not hasattr(self, "_decryptor") or getattr(self, "_decryptor_mode", None) != mode:
                try:
                    from discord.ext.voice_recv.reader import PacketDecryptor
                    self._decryptor = PacketDecryptor(mode, secret_key)
                    self._decryptor_mode = mode
                except Exception as e:
                    log.warning("[WATCHSTREAM] could not instantiate PacketDecryptor: %s", e)
                    self._decryptor = None

            if getattr(self, "_decryptor", None):
                try:
                    from discord.ext.voice_recv.rtp import RTPPacket
                    rtp_pkt = RTPPacket(data)
                    decrypted_payload = self._decryptor.decrypt_rtp(rtp_pkt)
                    if decrypted_payload:
                        data = rtp_pkt.header + decrypted_payload
                except Exception as e:
                    log.debug("[WATCHSTREAM] transport decrypt failed: %s", e)

        # Check payload type — skip Opus audio packets (typically PT 120 or 111)
        pt = data[1] & 0x7F
        if pt in (120, 111):
            return

        # Extract SSRC from 12-byte RTP header (bytes 8..11)
        ssrc = struct.unpack("!I", data[8:12])[0]

        if not hasattr(self, "_packet_count"):
            self._packet_count = 0
        self._packet_count += 1
        if self._packet_count == 1 or self._packet_count % 100 == 0:
            log.info(
                "[WATCHSTREAM] Video RTP packet #%d received (PT=%d, SSRC=%d, len=%d)",
                self._packet_count, pt, ssrc, len(data)
            )

        self.receiver.process_rtp_packet(
            rtp_data=data,
            dave_session=self.dave_session,
            ssrc=ssrc,
            user_id=self.target_user_id,
        )

    async def capture_snapshot(
        self,
        duration_sec: float = 4.0,
        filename: str = "latest_snapshot.jpg",
        max_wait_sec: float = 12.0,
    ) -> Optional[str]:
        """Captures a burst of RTP video packets for duration_sec seconds after waiting for stream handshake."""
        log.info(
            "[WATCHSTREAM] Initiating stream snapshot capture for target_user=%s (stream_key=%s)",
            self.target_user_id,
            self._stream_key,
        )
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
                log.warning("[WATCHSTREAM] connect() failed — aborting capture_snapshot")
                unregister_rtp_listener(self._on_udp_packet)
                return None

        self.receiver.start_capture()
        log.info("[WATCHSTREAM] Waiting for initial stream packets (up to %.1fs)...", max_wait_sec)

        # Wait until video packets start arriving from Discord Gateway (handshake sync)
        start_wait = time.monotonic()
        video_started = False
        while time.monotonic() - start_wait < max_wait_sec:
            if len(self.receiver._raw_nal_buffer) >= 2000:
                video_started = True
                log.info(
                    "[WATCHSTREAM] Stream video packets detected! Initial buffer: %d bytes. Collecting sample burst for %.1fs...",
                    len(self.receiver._raw_nal_buffer),
                    duration_sec,
                )
                break
            await asyncio.sleep(0.3)

        if not video_started and len(self.receiver._raw_nal_buffer) > 0:
            log.info(
                "[WATCHSTREAM] Proceeding with available buffer (%d bytes) after wait...",
                len(self.receiver._raw_nal_buffer),
            )

        # Collect sample burst for duration_sec
        await asyncio.sleep(duration_sec)
        self.receiver.stop_capture()

        # Unregister listener
        unregister_rtp_listener(self._on_udp_packet)
        if hasattr(vc_conn, "remove_socket_listener"):
            vc_conn.remove_socket_listener(self._on_udp_packet)
        elif socket_reader and hasattr(socket_reader, "unregister"):
            socket_reader.unregister(self._on_udp_packet)

        log.info(
            "[WATCHSTREAM] Sample burst finished. Collected %d bytes of raw NAL data. Extracting snapshot...",
            len(self.receiver._raw_nal_buffer),
        )

        # Extract snapshot JPEG
        jpg_path = self.receiver.extract_snapshot(duration_sec=duration_sec, filename=filename)
        log.info("[WATCHSTREAM] Snapshot extraction result: %s", jpg_path)
        return jpg_path

    async def disconnect(self) -> None:
        """Sends STREAM_SET_PAUSED: true to stop stream packet flow cleanly."""
        if not self._connected:
            return

        main_ws = self._bot.ws
        if main_ws:
            try:
                await _send_gateway_json(
                    main_ws,
                    {
                        "op": _OP_STREAM_SET_PAUSED,
                        "d": {
                            "stream_key": self._stream_key,
                            "paused": True,
                        },
                    },
                )
                log.info("[WATCHER] Sent STREAM_SET_PAUSED: true for %s", self._stream_key)
            except Exception as exc:
                log.warning("[WATCHER] Error disconnecting watcher: %s", exc)

        self._connected = False
