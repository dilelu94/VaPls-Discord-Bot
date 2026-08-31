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

        self.endpoint: Optional[str] = None
        self.token: Optional[str] = None
        self.ssrc: int = getattr(vc, "ssrc", 0)
        self.voice_port: Optional[int] = None
        self.endpoint_ip: Optional[str] = None
        self.ip: Optional[str] = None
        self.port: Optional[int] = None
        self.mode: str = ""
        self.secret_key: list[int] = []

        self.socket: Optional[socket.socket] = None
        self.ws: Optional[DiscordVoiceWebSocket] = None
        self._socket_reader: Optional[SocketReader] = None
        self._poll_task: Optional[asyncio.Task] = None

        self.dave_protocol_version: int = 0
        self.dave_pending_transitions: dict = {}
        self.dave_downgraded: bool = False

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

    @property
    def voice_client(self) -> discord.VoiceClient:
        return self._regular_vc

    @property
    def supported_modes(self):
        return type(self._regular_vc).supported_modes

    @property
    def max_dave_protocol_version(self) -> int:
        try:
            import davey_compat
            return davey_compat.DAVE_PROTOCOL_VERSION
        except Exception:
            return 1

    @property
    def can_encrypt(self) -> bool:
        return (
            self.dave_protocol_version != 0
            and self.dave_session is not None
            and getattr(self.dave_session, "ready", False)
        )

    async def reinit_dave_session(self, force: bool = False) -> None:
        if self.dave_protocol_version > 0 and self.server_id:
            dave_channel_id = self.server_id - 1
            if self.dave_session is not None:
                self.dave_session.reinit(
                    self.dave_protocol_version, self.user.id, dave_channel_id
                )
            else:
                try:
                    import davey_compat
                    self.dave_session = davey_compat.DaveSession(
                        self.dave_protocol_version, self.user.id, dave_channel_id
                    )
                    self.dave_session._voice_state = self
                except Exception as exc:
                    log.warning("[WATCHER] DaveSession init error: %s", exc)
            if self.dave_session is not None and self.ws:
                try:
                    from discord.gateway import DiscordVoiceWebSocket
                    await self.ws.send_binary(
                        DiscordVoiceWebSocket.MLS_KEY_PACKAGE,
                        self.dave_session.get_serialized_key_package(),
                    )
                except Exception as exc:
                    log.warning("[WATCHER] Could not send MLS_KEY_PACKAGE: %s", exc)
        elif self.dave_session:
            self.dave_session.reset()
            self.dave_session.set_passthrough_mode(True, 10)

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        if self.ws:
            await self.ws.send_as_json(
                {
                    "op": DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME,
                    "d": {"transition_id": transition_id},
                }
            )
            await self.reinit_dave_session(force=True)

    async def _execute_transition(self, transition_id: int) -> None:
        if transition_id not in self.dave_pending_transitions:
            return
        old_version = self.dave_protocol_version
        self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)
        if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
            self.dave_downgraded = True
        elif transition_id > 0 and self.dave_downgraded:
            self.dave_downgraded = False
            if self.dave_session:
                self.dave_session.set_passthrough_mode(True, 10)

    def add_socket_listener(self, callback) -> None:
        if self._socket_reader is not None:
            self._socket_reader.register(callback)

    def remove_socket_listener(self, callback) -> None:
        if self._socket_reader is not None:
            self._socket_reader.unregister(callback)

    def send_packet(self, packet: bytes) -> None:
        if self.socket:
            try:
                self.socket.sendall(packet)
            except OSError:
                pass

    async def connect(self, timeout: float = 20.0) -> bool:
        """Discreetly sends STREAM_WATCH and STREAM_SET_PAUSED to trigger stream packet flow."""
        main_ws = self._bot.ws
        if not main_ws:
            log.warning("[WATCHER] Main bot websocket is not connected")
            return False

        # Register gateway event futures BEFORE sending op 20 to avoid losing
        # events that arrive before we start listening.
        stream_key = self._stream_key
        create_fut = main_ws.wait_for(
            "STREAM_CREATE",
            predicate=lambda d: d.get("stream_key", "") == stream_key,
        )
        server_fut = main_ws.wait_for(
            "STREAM_SERVER_UPDATE",
            predicate=lambda d: d.get("stream_key", "") == stream_key,
        )

        log.info(
            "[WATCHER] Requesting STREAM_WATCH for stream_key=%s",
            self._stream_key,
        )

        try:
            # 1. Send Opcode 20 (STREAM_WATCH)
            await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_WATCH,
                    "d": {
                        "stream_key": self._stream_key,
                        "video_codec": "H264",
                    },
                },
            )

            # 2. Send Opcode 22 (STREAM_SET_PAUSED: false) on Gateway WS
            await _send_gateway_json(
                main_ws,
                {
                    "op": _OP_STREAM_SET_PAUSED,
                    "d": {
                        "stream_key": self._stream_key,
                        "paused": False,
                    },
                },
            )

            # 3. Send Opcode 22 (STREAM_SET_PAUSED: false) on Voice WS if available
            voice_ws = getattr(self._regular_vc, "ws", None) or getattr(getattr(self._regular_vc, "_connection", None), "ws", None)
            if voice_ws:
                await _send_gateway_json(
                    voice_ws,
                    {
                        "op": 22,
                        "d": {
                            "stream_key": self._stream_key,
                            "paused": False,
                        },
                    },
                )

            # 4. Connect dedicated stream WebSocket & UDP socket if STREAM_SERVER_UPDATE is received
            try:
                server_data = await asyncio.wait_for(server_fut, timeout=5.0)
                endpoint = server_data.get("endpoint", "")
                if endpoint.startswith("wss://"):
                    endpoint = endpoint[6:]
                self.endpoint = endpoint
                self.token = server_data.get("token")
                log.info("[WATCHER] Received STREAM_SERVER_UPDATE: endpoint=%s", self.endpoint)

                create_data = await asyncio.wait_for(create_fut, timeout=3.0)
                self.server_id = int(create_data["rtc_server_id"])

                if self.endpoint and DiscordVoiceWebSocket is not None:
                    self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self.socket.setblocking(False)
                    self._socket_reader = SocketReader(self, start_paused=False)
                    self._socket_reader.register(self._on_udp_packet)
                    self._socket_reader.start()

                    self.ws = await DiscordVoiceWebSocket.from_connection_state(
                        self, resume=False
                    )
                    while not self.ip:
                        await self.ws.poll_event()

                    if self.endpoint_ip and self.voice_port:
                        self.socket.connect((self.endpoint_ip, self.voice_port))

                    while self.ws.secret_key is None:
                        await self.ws.poll_event()

                    self.mode = getattr(self.ws, "mode", "") or "aead_xchacha20_poly1305_rtpsize"
                    self.secret_key = getattr(self.ws, "secret_key", None) or []

                    await self.ws.client_connect()
                    loop = asyncio.get_event_loop()
                    self._poll_task = loop.create_task(self._poll_ws(), name="watcher-ws-poll")
                    log.info("[WATCHER] Dedicated stream socket connection established to %s (mode=%s)", self.endpoint, self.mode)
            except Exception as exc:
                log.info("[WATCHER] Dedicated stream connection note: %s (using main voice listener fallback)", exc)

            self._connected = True
            log.info("[WATCHER] STREAM_WATCH signal sent (status=True)")
            return True
        except Exception as exc:
            log.error("[WATCHER] Failed to send STREAM_WATCH: %s", exc, exc_info=True)
            return False

    async def _poll_ws(self) -> None:
        """Continuously poll the stream WebSocket to handle heartbeats."""
        try:
            while self.ws:
                await self.ws.poll_event()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("[WATCHER] Stream WS poller ended: %s", exc)

    def _on_udp_packet(self, data: bytes) -> None:
        """UDP Socket callback for incoming RTP packets."""
        if not data or len(data) < 12:
            return

        # 0. Apply Discord RTP transport decryption if key/mode is present
        vc = self._regular_vc
        mode = getattr(self, "mode", None) or getattr(getattr(self, "ws", None), "mode", None) or getattr(vc, "mode", None) or getattr(getattr(vc, "_connection", None), "mode", None)
        secret_key = getattr(self, "secret_key", None) or getattr(getattr(self, "ws", None), "secret_key", None) or getattr(vc, "secret_key", None) or getattr(getattr(vc, "_connection", None), "secret_key", None)

        is_decrypted = True
        if isinstance(mode, str) and secret_key and isinstance(secret_key, (bytes, list, tuple)):
            secret_key = bytes(secret_key)
            is_decrypted = False
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
                        hdr = bytearray(rtp_pkt.header)
                        hdr[0] &= ~0x10  # Clear extension bit since decrypt_rtp already processed ext headers
                        data = bytes(hdr) + decrypted_payload
                        is_decrypted = True
                except Exception as e:
                    log.debug("[WATCHSTREAM] transport decrypt failed: %s", e)

        if not is_decrypted:
            return

        # Check payload type — skip Opus audio (120, 111, 121, 77) and RTCP (72-76, 200-204)
        pt = data[1] & 0x7F
        if pt in (120, 111, 121, 77) or 72 <= pt <= 76 or 200 <= pt <= 204:
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
        duration_sec: float = 6.0,
        filename: str = "latest_snapshot.jpg",
        max_wait_sec: float = 15.0,
    ) -> Optional[str]:
        """Captures a burst of RTP video packets for duration_sec seconds after waiting for stream handshake."""
        log.info(
            "[WATCHSTREAM] Initiating stream snapshot capture for target_user=%s (stream_key=%s)",
            self.target_user_id,
            self._stream_key,
        )
        # Bind listener to VoiceRecvClient reader thread (_reader._socket_listeners)
        register_rtp_listener(self._on_udp_packet)
        vc_conn = getattr(self._regular_vc, "_connection", None) or self._regular_vc
        reader = (
            getattr(self._regular_vc, "_reader", None)
            or getattr(vc_conn, "_reader", None)
            or getattr(vc_conn, "_socket_reader", None)
        )

        if reader is not None:
            if hasattr(reader, "add_socket_listener") and callable(reader.add_socket_listener):
                reader.add_socket_listener(self._on_udp_packet)
            elif hasattr(reader, "_socket_listeners") and isinstance(reader._socket_listeners, list):
                if self._on_udp_packet not in reader._socket_listeners:
                    reader._socket_listeners.append(self._on_udp_packet)
            elif hasattr(reader, "register") and callable(reader.register):
                reader.register(self._on_udp_packet)
        elif hasattr(vc_conn, "add_socket_listener"):
            vc_conn.add_socket_listener(self._on_udp_packet)

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
            if (
                len(self.receiver._raw_nal_buffer) >= 15000
                and self.receiver._sps_nal
                and self.receiver._pps_nal
            ):
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
        snapshot_path = self.receiver.extract_snapshot(filename=filename)
        log.info("[WATCHSTREAM] Snapshot extraction result: %s", snapshot_path)
        return snapshot_path

    async def record_and_decode_stream(
        self,
        duration_sec: float = 10.0,
        h264_filename: str = "recorded_stream.h264",
        mp4_filename: str = "recorded_stream.mp4",
        max_wait_sec: float = 12.0,
    ) -> Optional[str]:
        """Captures a 10-second burst of RTP video packets and converts them into an MP4 video file."""
        log.info(
            "[WATCHSTREAM] Initiating 10s video recording for target_user=%s (stream_key=%s)",
            self.target_user_id,
            self._stream_key,
        )
        # Bind listener to VoiceRecvClient reader thread (_reader._socket_listeners)
        register_rtp_listener(self._on_udp_packet)
        vc_conn = getattr(self._regular_vc, "_connection", None) or self._regular_vc
        reader = (
            getattr(self._regular_vc, "_reader", None)
            or getattr(vc_conn, "_reader", None)
            or getattr(vc_conn, "_socket_reader", None)
        )

        if reader is not None:
            if hasattr(reader, "add_socket_listener") and callable(reader.add_socket_listener):
                reader.add_socket_listener(self._on_udp_packet)
            elif hasattr(reader, "_socket_listeners") and isinstance(reader._socket_listeners, list):
                if self._on_udp_packet not in reader._socket_listeners:
                    reader._socket_listeners.append(self._on_udp_packet)
            elif hasattr(reader, "register") and callable(reader.register):
                reader.register(self._on_udp_packet)
        elif hasattr(vc_conn, "add_socket_listener"):
            vc_conn.add_socket_listener(self._on_udp_packet)

        if not self._connected:
            ok = await self.connect()
            if not ok:
                log.warning("[WATCHSTREAM] connect() failed — aborting record_and_decode_stream")
                unregister_rtp_listener(self._on_udp_packet)
                return None

        self.receiver.start_capture()
        log.info("[WATCHSTREAM] Waiting for initial stream packets (up to %.1fs)...", max_wait_sec)

        start_wait = time.monotonic()
        video_started = False
        while time.monotonic() - start_wait < max_wait_sec:
            if (
                len(self.receiver._raw_nal_buffer) >= 15000
                and self.receiver._sps_nal
                and self.receiver._pps_nal
            ):
                video_started = True
                log.info(
                    "[WATCHSTREAM] Stream video packets detected! Initial buffer: %d bytes. Recording sample for %.1fs...",
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

        await asyncio.sleep(duration_sec)
        self.receiver.stop_capture()

        unregister_rtp_listener(self._on_udp_packet)
        if hasattr(vc_conn, "remove_socket_listener"):
            vc_conn.remove_socket_listener(self._on_udp_packet)
        elif socket_reader and hasattr(socket_reader, "unregister"):
            socket_reader.unregister(self._on_udp_packet)

        log.info(
            "[WATCHSTREAM] Recording burst finished. Collected %d bytes of raw NAL data. Decoding to MP4...",
            len(self.receiver._raw_nal_buffer),
        )

        mp4_path = self.receiver.convert_sample_to_mp4(h264_filename=h264_filename, mp4_filename=mp4_filename)
        log.info("[WATCHSTREAM] MP4 video recording result: %s", mp4_path)
        return mp4_path

    async def disconnect(self) -> None:
        """Sends STREAM_SET_PAUSED: true to stop stream packet flow cleanly."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

        if self._socket_reader is not None:
            self._socket_reader.stop()
            self._socket_reader = None

        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass

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
